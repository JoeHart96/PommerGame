import os
import types

import gym
import numpy as np
import torch
from gym.spaces.box import Box

from helpers.monitor import Monitor
from helpers.atari_wrappers import make_atari, wrap_deepmind
from helpers.vec_env import VecEnvWrapper
from helpers.vec_env.dummy_vec_env import DummyVecEnv
from helpers.vec_env.subproc_vec_env import SubprocVecEnv
from helpers.vec_env.vec_normalize import VecNormalize
from envs.pommerman import PommermanEnvWrapper

try:
    import dm_control2gym
except ImportError:
    pass

try:
    import roboschool
except ImportError:
    pass

try:
    import pybullet_envs
except ImportError:
    pass

try:
    import envs.pommerman
except ImportError:
    pass

from graphic_pomme_env import graphic_pomme_env
from graphic_pomme_env.wrappers import PommerEnvWrapperFrameSkip2


class RawObsEnvWrapper(gym.Wrapper):
    def __init__(self, env=None):
        super(RawObsEnvWrapper, self).__init__(env)
        self._board_size = env._board_size
        self.training_agent = env.training_agent

    def step(self, actions):
        state, reward, done, _ = self.env.step(actions)
        state = self.env.get_last_step_raw()
        return state, reward, done, {}

    def get_observations(self):
        return self.env.get_observations()

    def act(self, obs):
        return self.env.act(obs)

    def reset(self):
        self.env.reset()
        return self.env.get_last_step_raw()


def make_env(env_id, seed, rank, log_dir=None, add_timestep=False, allow_early_resets=False, random_position=0):
    def _thunk():
        if random_position:
            env = PommerEnvWrapperFrameSkip2(
                num_stack=5, start_pos=np.random.choice([0, 1]), opponent_actor=None, board='GraphicOVOCompact-v0'
            )
        else:
            env = PommerEnvWrapperFrameSkip2(
                num_stack=5, start_pos=0, opponent_actor=None, board='GraphicOVOCompact-v0'
            )
        # hacky af
        obs, opp_obs = env.reset()
        env.training_agent = 0
        env.env.training_agent = 0
        env.action_space = env.env.action_space
        env.observation_space = env.env.observation_space
        env.reward_range = env.env.reward_range
        env.metadata = env.env.metadata
        env._board_size = env.env._board_size
        env.spec = env.env.spec
        # env.get_observations = lambda: env.env.get_observations()
        env = PommermanEnvWrapper(env, feature_config=None, obs_shape=obs.shape)

        env.seed(seed + rank)

        obs_shape = env.observation_space.shape
        if add_timestep and len(obs_shape) == 1 and str(env).find('TimeLimit') > -1:
            env = AddTimestep(env)

        if log_dir is not None:
            env = Monitor(env, os.path.join(log_dir, str(rank)),
                          allow_early_resets=allow_early_resets)

        # If the input has shape (W,H,3), wrap for PyTorch convolutions
        obs_shape = env.observation_space.shape
        if len(obs_shape) == 3 and obs_shape[2] in [1, 3]:
            env = TransposeImage(env)

        return env

    return _thunk


def make_vec_envs(env_name, seed, num_processes, gamma, no_norm, num_stack,
                  log_dir=None, add_timestep=False, device='cpu', allow_early_resets=False, eval=False,
                  random_start_position=False):
    envs = [make_env(env_name, seed, i, log_dir, add_timestep, allow_early_resets, random_start_position) for i in range(num_processes)]

    if len(envs) > 1:
        envs = SubprocVecEnv(envs)
    else:
        envs = DummyVecEnv(envs)

    if not no_norm and len(envs.observation_space.shape) == 1:
        if gamma is None:
            envs = VecNormalize(envs, ret=False)
        else:
            envs = VecNormalize(envs, gamma=gamma)
        if eval:
            # An ugly hack to remove updates
            def _obfilt(self, obs):
                if self.ob_rms:
                    obs = np.clip((obs - self.ob_rms.mean) / np.sqrt(self.ob_rms.var + self.epsilon),
                                  -self.clipob, self.clipob)
                    return obs
                else:
                    return obs

            envs._obfilt = types.MethodType(_obfilt, envs)

    envs = VecPyTorch(envs, device)

    if num_stack > 1:
        envs = VecPyTorchFrameStack(envs, num_stack, device)

    return envs


class AddTimestep(gym.ObservationWrapper):
    def __init__(self, env=None):
        super(AddTimestep, self).__init__(env)
        self.observation_space = Box(
            self.observation_space.low[0],
            self.observation_space.high[0],
            [self.observation_space.shape[0] + 1],
            dtype=self.observation_space.dtype)

    def observation(self, observation):
        return np.concatenate((observation, [self.env._elapsed_steps]))


class TransposeImage(gym.ObservationWrapper):
    def __init__(self, env=None):
        super(TransposeImage, self).__init__(env)
        obs_shape = self.observation_space.shape
        self.observation_space = Box(
            self.observation_space.low[0, 0, 0],
            self.observation_space.high[0, 0, 0],
            [obs_shape[2], obs_shape[1], obs_shape[0]],
            dtype=self.observation_space.dtype)

    def observation(self, observation):
        return observation.transpose(2, 0, 1)


class VecPyTorch(VecEnvWrapper):
    def __init__(self, venv, device):
        """Return only every `skip`-th frame"""
        super(VecPyTorch, self).__init__(venv)
        self.device = device
        # TODO: Fix data types

    def reset(self):
        obs = self.venv.reset()
        obs = torch.from_numpy(obs).float().to(self.device)
        return obs

    def step_async(self, actions):
        actions = actions.squeeze(1).cpu().numpy()
        self.venv.step_async(actions)

    def step_wait(self):
        obs, reward, done, info = self.venv.step_wait()
        obs = torch.from_numpy(obs).float().to(self.device)
        reward = torch.from_numpy(np.expand_dims(np.stack(reward), 1)).float()
        return obs, reward, done, info


# Derived from
# https://github.com/openai/baselines/blob/master/baselines/common/vec_env/vec_frame_stack.py
class VecPyTorchFrameStack(VecEnvWrapper):
    def __init__(self, venv, nstack, device):
        self.venv = venv
        self.nstack = nstack
        wos = venv.observation_space  # wrapped ob space
        self.shape_dim0 = wos.low.shape[0]
        low = np.repeat(wos.low, self.nstack, axis=0)
        high = np.repeat(wos.high, self.nstack, axis=0)
        self.stackedobs = np.zeros((venv.num_envs,) + low.shape)
        self.stackedobs = torch.from_numpy(self.stackedobs).float()
        self.stackedobs = self.stackedobs.to(device)
        observation_space = gym.spaces.Box(low=low, high=high, dtype=venv.observation_space.dtype)
        VecEnvWrapper.__init__(self, venv, observation_space=observation_space)

    def step_wait(self):
        obs, rews, news, infos = self.venv.step_wait()
        self.stackedobs[:, :-self.shape_dim0] = self.stackedobs[:, self.shape_dim0:]
        for (i, new) in enumerate(news):
            if new:
                self.stackedobs[i] = 0
        self.stackedobs[:, -self.shape_dim0:] = obs
        return self.stackedobs, rews, news, infos

    def reset(self):
        obs = self.venv.reset()
        self.stackedobs.fill_(0)
        self.stackedobs[:, -self.shape_dim0:] = obs
        return self.stackedobs

    def close(self):
        self.venv.close()
