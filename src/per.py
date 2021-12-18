#######################################
############# SUBJECT #################
#######################################
# In this file you can find the implementation of
# Prioritized Experience Replay (PER) in PyTorch, 
# with the highway-v0 envirement.
# CNN models can solve the task purely by looking at the scene,
# so we’ll use a patch of the screen as an input
# This slows down the training, because we have to render all the frames.

import gym
import math
import random
import numpy as np
import matplotlib
import matplotlib.pyplot as plt
from collections import namedtuple, deque
from itertools import count
from PIL import Image
from torch.optim import Adam
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import torchvision.transforms as T
import highway_env
env = gym.make('highway-v0').unwrapped

# set up matplotlib
is_ipython = 'inline' in matplotlib.get_backend()
if is_ipython:
    from IPython import display

plt.ion()

# if gpu is to be used
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
USE_CUDA = torch.cuda.is_available()

#here we define our transition (state, action, reward, next_state, done)
# It essentially maps (state, action) pairs
# to their (next_state, reward) result, with the state being the
# screen difference image as described later on.
Transition = namedtuple('Transition',
                        ('state', 'action', 'reward', 'next_state','done'))


# This class implements a simple memory buffer replay with priority on experiences
# We use this technique to compute loss on batches with no correlated states, and with prioritized experiences
class PER(object):
    def __init__(self, capacity, prob_alpha=0.6):
        self.prob_alpha = prob_alpha
        self.capacity   = capacity
        self.memory     = []
        self.pos        = 0
        self.priorities = np.zeros((capacity,), dtype=np.float64)
    
    def push(self, *args):
        max_prio = self.priorities.max() if self.memory else 1.0
        
        if len(self.memory) < self.capacity:
            self.memory.append(Transition(*args))
        else:
            self.memory[self.pos] = Transition(*args)
        
        self.priorities[self.pos] = max_prio
        self.pos = (self.pos + 1) % self.capacity
    
    def sample(self, beta=0.4):
        """Sample batch_size of experiences that have more priority."""
        if len(self.memory) == self.capacity:
            prios = self.priorities
        else:
            prios = self.priorities[:self.pos]
        
        probs  = prios ** self.prob_alpha
        probs /= probs.sum()
        
        indices = np.random.choice(len(self.memory), BATCH_SIZE, p=probs)
        total    = len(self.memory)
        weights  = (total * probs[indices]) ** (-beta)
        weights /= weights.max()
        weights  = np.array(weights, dtype=np.float32)
        
        transitions = [self.memory[idx] for idx in indices]
        batch       = Transition(*zip(*transitions))
        states      = torch.cat(batch.state)
        actions     = torch.cat(batch.action)
        rewards     = torch.cat(batch.reward)
        next_states = batch.next_state
        dones       = batch.done
        
        return states, actions, rewards, next_states, dones, indices, weights
    
    def update_priorities(self, batch_indices, batch_priorities):
        """Update the priorities every time we calculate a new loss"""
        for idx, prio in zip(batch_indices, batch_priorities):
            self.priorities[idx] = prio[0]

    def __len__(self):
        return len(self.memory)

beta_start = 0.4
beta_frames = 1000 
beta_by_frame = lambda frame_idx: min(1.0, beta_start + frame_idx * (1.0 - beta_start) / beta_frames)


# DQN algorithm
# -------------
# Input: The difference between the current and previous screen patches
# Output: Different actions
class DQN(nn.Module):

    def __init__(self, h, w, outputs):
        super(DQN, self).__init__()
        self.conv1 = nn.Conv2d(3, 16, kernel_size=5, stride=2)
        self.bn1 = nn.BatchNorm2d(16)
        self.conv2 = nn.Conv2d(16, 32, kernel_size=5, stride=2)
        self.bn2 = nn.BatchNorm2d(32)
        self.conv3 = nn.Conv2d(32, 32, kernel_size=5, stride=2)
        self.bn3 = nn.BatchNorm2d(32)


        def conv2d_size_out(size, kernel_size = 5, stride = 2):
            return (size - (kernel_size - 1) - 1) // stride  + 1
        convw = conv2d_size_out(conv2d_size_out(conv2d_size_out(w)))
        convh = conv2d_size_out(conv2d_size_out(conv2d_size_out(h)))
        linear_input_size = convw * convh * 32
        self.head = nn.Linear(linear_input_size, outputs)

    def forward(self, x):
        x = x.to(device)
        x = F.relu(self.bn1(self.conv1(x)))
        x = F.relu(self.bn2(self.conv2(x)))
        x = F.relu(self.bn3(self.conv3(x)))
        return self.head(x.view(x.size(0), -1))



# Input extraction
# ----------------
resize = T.Compose([T.ToPILImage(),
                    T.Resize(100, interpolation=T.InterpolationMode.BICUBIC),
                    T.ToTensor()])


def get_screen():
    # Returned screen requested by gym is 600x150x3
    screen = env.render(mode='rgb_array').transpose((2, 0, 1))
    screen = np.ascontiguousarray(screen, dtype=np.float32) / 255
    screen = torch.from_numpy(screen)
    return resize(screen).unsqueeze(0)


env.reset()
plt.figure()
plt.imshow(get_screen().cpu().squeeze(0).permute(1, 2, 0).numpy(),
           interpolation='none')
plt.title('Example extracted screen')
plt.show()


# Training Hyperparameters
# ------------------------
learning_rate = 5e-4
BATCH_SIZE = 32
GAMMA = 0.8
EPS_START = 0.9
EPS_END = 0.05
EPS_DECAY = 200
TARGET_UPDATE = 50


init_screen = get_screen()
_, _, screen_height, screen_width = init_screen.shape

# Get number of actions from gym action space
n_actions = env.action_space.n

# initialize our two networks
# ---------------------------
policy_net = DQN(screen_height, screen_width, n_actions).to(device)
target_net = DQN(screen_height, screen_width, n_actions).to(device)
target_net.load_state_dict(policy_net.state_dict())
target_net.eval()

optimizer = Adam(policy_net.parameters(),lr = learning_rate)
replay_initial = 10000
per_memory = PER(10000)

steps_done = 0

# chose an action with epsilon-greedy 
# -----------------------------------
def select_action(state):
    global steps_done
    sample = random.random()
    eps_threshold = EPS_END + (EPS_START - EPS_END) * \
        math.exp(-1. * steps_done / EPS_DECAY)
    steps_done += 1
    if sample > eps_threshold:
        with torch.no_grad():
            return policy_net(state).max(1)[1].view(1, 1)
    else:
        return torch.tensor([[random.randrange(n_actions)]], device=device, dtype=torch.long)


episode_durations = []


def plot_durations():
    plt.figure(2)
    plt.clf()
    durations_t = torch.tensor(episode_durations, dtype=torch.float)
    plt.title('Training...')
    plt.xlabel('Episode')
    plt.ylabel('Duration')
    plt.plot(durations_t.numpy())
    # Take 100 episode averages and plot them too
    if len(durations_t) >= 100:
        means = durations_t.unfold(0, 100, 1).mean(1).view(-1)
        means = torch.cat((torch.zeros(99), means))
        plt.plot(means.numpy())

    plt.pause(0.001)  # pause a bit so that plots are updated
    if is_ipython:
        display.clear_output(wait=True)
        display.display(plt.gcf())


# function to compute the loss and optimize the model weights/bias
# ----------------------------------------------------------------
def optimize_model(beta):
    if len(per_memory) < BATCH_SIZE:
        return
    state, action, reward, next_state, done, indices, weights = per_memory.sample(beta) 

    # Compute a mask of non-final states and concatenate the batch elements
    non_final_mask = torch.tensor(tuple(map(lambda s: s is not None,
                                          next_state)), device=device, dtype=torch.bool)
    non_final_next_states = torch.cat([s for s in next_state
                                                if s is not None])

    q_values      = policy_net(state)
    # Compute the Q values
    q_value          = q_values.gather(1, action)
    next_q_values = torch.zeros(BATCH_SIZE, device=device)
    with torch.no_grad():
        next_q_values[non_final_mask] = target_net(non_final_next_states).max(1)[0].detach()
    # Compute the expected Q values
    expected_q_value = (next_q_values * GAMMA) + reward
    # Compute the loss and priorities
    loss  = (q_value - expected_q_value.unsqueeze(1)).pow(2)*torch.as_tensor(weights)
    prios = loss.detach() + 1e-5
    loss  = loss.mean()

    # Optimize the model
    optimizer.zero_grad()
    loss.backward()
    
    #update priorities
    per_memory.update_priorities(indices, prios.data.cpu().numpy())
    optimizer.step()
    return loss


# Training loop
# -------------
def modele():
    num_episodes = 400
    total_reward = 0
    for i_episode in range(num_episodes):
        # Initialize the environment and state
        env.reset()
        last_screen = get_screen()
        current_screen = get_screen()
        state = current_screen - last_screen    
        for t in count():
            # Select and perform an action
            action = select_action(state)
            _, reward, done, _ = env.step(action.item())
            total_reward += reward
            reward = torch.tensor([reward], device=device)
            # Observe new state
            last_screen = current_screen
            current_screen = get_screen()
            if not done:
                next_state = current_screen - last_screen
            else:
                next_state = None

            # Store the transition in per_memory
            per_memory.push(state, action, reward, next_state, done)

            # Move to the next state
            state = next_state

            # Perform one step of the optimization (on the policy network)
            beta = beta_by_frame(steps_done)
            optimize_model(beta)
            if done:
                episode_durations.append(t + 1)
                plot_durations()
                break
                
        if i_episode % 20 == 0:
            print(f"Mean episode {i_episode}/400 reward is:{total_reward / 20:.2f}")
            total_reward = 0
        # Update the target network, copying all weights and biases in DQN
        if i_episode % TARGET_UPDATE == 0:
            target_net.load_state_dict(policy_net.state_dict())
        


# Train the model and save it
# ---------------------------

# modele()
# torch.save(policy_net.state_dict(), "../models/model_per_400")

def run_episode(env, render = False):
    env.reset()
    last_screen = get_screen()
    current_screen = get_screen()
    state = current_screen - last_screen
    total_reward = 0
    for t in count():
        if render:
            env.render()
        # Select and perform an action
        action = select_action(state)
        _, reward, done, _ = env.step(action.item())
        total_reward += reward

        # Observe new state
        last_screen = current_screen
        current_screen = get_screen()
        if not done:
            next_state = current_screen - last_screen
        else:
            next_state = None

        # Move to the next state
        state = next_state

        if done:
            break
    return total_reward


# In case the modele is already trained we simply load it
steps_done = 1000000000000
policy_net.load_state_dict(torch.load("../models/model_per_400"))
policy_scores = [run_episode(env,True) for _ in range(50)]
print("Average score of the policy: ", np.mean(policy_scores))
env.close()
