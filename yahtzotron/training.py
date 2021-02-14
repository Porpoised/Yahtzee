from collections import deque, defaultdict

import tqdm
import numpy as np
from loguru import logger

import jax
import jax.numpy as jnp
import optax
import rlax

from yahtzotron.game import play_tournament
from yahtzotron.interactive import print_score

REWARD_NORM = 100
WINNING_REWARD = 100

MINIMUM_LOGIT = jnp.finfo(jnp.float32).min


def entropy(logits):
    probs = jax.nn.softmax(logits)
    logprobs = jax.nn.log_softmax(logits)
    return -jnp.sum(probs * logprobs, axis=-1)


def cross_entropy(logits, actions):
    logprob = jax.nn.log_softmax(logits)
    labels = jax.nn.one_hot(actions, logits.shape[-1])
    return -jnp.sum(labels * logprob, axis=1)


def compile_loss_function(type_, network):
    def loss(
        weights,
        observations,
        keep_actions,
        cat_actions,
        rewards,
        td_lambda=0.2,
        discount=0.99,
        policy_cost=0.25,
        entropy_cost=1e-3,
    ):
        """Actor-critic loss."""
        rolls_left = observations[..., 0]
        keep_logits, cat_logits, values = network(weights, observations)
        values = jnp.append(values, jnp.sum(rewards))

        td_errors = rlax.td_lambda(
            v_tm1=values[:-1],
            r_t=rewards,
            discount_t=jnp.full_like(rewards, discount),
            v_t=values[1:],
            lambda_=td_lambda,
        )
        critic_loss = jnp.mean(td_errors ** 2)

        def pad_logit(logit, num_pad):
            return jnp.pad(logit, [(0, 0), (0, num_pad)], constant_values=MINIMUM_LOGIT)

        logit_shapediff = cat_logits.shape[1] - keep_logits.shape[1]
        if logit_shapediff > 0:
            keep_logits = pad_logit(keep_logits, logit_shapediff)
        elif logit_shapediff < 0:
            cat_logits = pad_logit(cat_logits, -logit_shapediff)

        pertinent_mask = rolls_left == 0
        pertinent_logits = jnp.where(
            pertinent_mask.reshape(-1, 1), cat_logits, keep_logits
        )
        pertinent_logits = jnp.maximum(pertinent_logits, MINIMUM_LOGIT)
        pertinent_actions = jnp.where(pertinent_mask, cat_actions, keep_actions)

        if type_ == "a2c":
            actor_loss = rlax.policy_gradient_loss(
                logits_t=pertinent_logits,
                a_t=pertinent_actions,
                adv_t=td_errors,
                w_t=jnp.ones(td_errors.shape[0]),
            )
        elif type_ == "supervised":
            actor_loss = jnp.mean(cross_entropy(pertinent_logits, pertinent_actions))

        entropy_loss = -jnp.mean(entropy(pertinent_logits))

        return policy_cost * actor_loss, critic_loss, entropy_cost * entropy_loss

    return jax.jit(loss)


def compile_sgd_step(loss_func, optimizer):
    def sgd_step(
        weights,
        opt_state,
        observations,
        keep_actions,
        cat_actions,
        rewards,
        **loss_kwargs
    ):
        """Does a step of SGD over a trajectory."""
        total_loss = lambda *args: sum(loss_func(*args, **loss_kwargs))
        gradients = jax.grad(total_loss)(
            weights, observations, keep_actions, cat_actions, rewards
        )
        updates, opt_state = optimizer.update(gradients, opt_state)
        weights = optax.apply_updates(weights, updates)
        return weights, opt_state

    return jax.jit(sgd_step)


def train_a2c(
    base_agent,
    num_epochs,
    checkpoint_path=None,
    players_per_game=4,
    learning_rate=1e-3,
    entropy_cost=1e-3,
    pretraining=False,
):
    """Train advantage actor-critic (A2C) agent through self-play"""
    objective = base_agent._objective

    optimizer = optax.MultiSteps(
        optax.adam(learning_rate), players_per_game, use_grad_mean=False
    )
    opt_state = optimizer.init(base_agent.get_weights())

    running_stats = defaultdict(lambda: deque(maxlen=1000))
    progress = tqdm.tqdm(range(num_epochs), dynamic_ncols=True)

    loss_type = "supervised" if pretraining else "a2c"
    loss_fn = compile_loss_function(loss_type, base_agent._network)
    loss_kwargs = dict(entropy_cost=entropy_cost)
    sgd_step = compile_sgd_step(loss_fn, optimizer)

    best_score = -float("inf")

    if pretraining:
        greedy_agent = base_agent.clone()
        greedy_agent._default_greedy = True
        agents = [greedy_agent] * players_per_game
    else:
        agents = [base_agent] * players_per_game

    for i in progress:
        scores, trajectories = play_tournament(agents, record_trajectories=True)

        final_scores = [s.total_score() for s in scores]
        winner = np.argmax(final_scores)
        logger.info(
            "Player {} won with a score of {} (median {})",
            winner,
            final_scores[winner],
            np.median(final_scores),
        )
        logger.info(
            " Winning scorecard:\n{}",
            print_score(scores[winner]),
        )

        weights = base_agent._weights

        for p in range(players_per_game):
            observations, keep_actions, cat_actions, rewards = zip(*trajectories[p])
            assert sum(rewards) == scores[p].total_score()

            observations = np.stack(observations, axis=0)
            keep_actions = np.array(keep_actions, dtype=np.int32)
            cat_actions = np.array(cat_actions, dtype=np.int32)
            rewards = np.array(rewards, dtype=np.float32) / REWARD_NORM

            logger.debug(" rewards {}: {}", p, rewards)

            if objective == "win" and p == winner:
                rewards[-1] += WINNING_REWARD / REWARD_NORM

            weights, opt_state = sgd_step(
                weights,
                opt_state,
                observations,
                keep_actions,
                cat_actions,
                rewards,
                **loss_kwargs
            )

            loss_components = loss_fn(
                weights, observations, keep_actions, cat_actions, rewards, **loss_kwargs
            )
            loss_components = [float(k) for k in loss_components]

            epoch_stats = dict(
                actor_loss=loss_components[0],
                critic_loss=loss_components[1],
                entropy_loss=loss_components[2],
                loss=sum(loss_components),
                score=scores[p].total_score(),
            )
            for key, val in epoch_stats.items():
                buf = running_stats[key]
                if len(buf) == buf.maxlen:
                    buf.popleft()
                buf.append(val)

        base_agent.set_weights(weights)

        if pretraining:
            greedy_agent.set_weights(weights)

        if i % 10 == 0:
            avg_score = np.mean(running_stats["score"])
            if avg_score > best_score + 1 and i > running_stats["score"].maxlen:
                best_score = avg_score

                if checkpoint_path is not None:
                    logger.warning(
                        " Saving checkpoint for average score {:.2f}", avg_score
                    )
                    base_agent.save(checkpoint_path)

            progress.set_postfix(
                {key: np.mean(val) for key, val in running_stats.items()}
            )

    return base_agent
