'''
Code for simulations presented in
"Deep learning with segregated dendrites"
by Jordan Guergiuev, Timothy P. Lillicrap, Blake A. Richards.

     Author: Jordan Guergiuev
     E-mail: guerguiev.j@gmail.com
       Date: March 3, 2017
Institution: University of Toronto Scarborough
'''

from __future__ import print_function
import numpy as np
import matplotlib
matplotlib.use('TkAgg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gs
import copy
import datetime
import os
import pdb
import sys
import time
import shutil
import json

if sys.version_info >= (3,):
    xrange = range

n_full_test  = 10000 # number of examples to use for full tests  (every epoch)
n_quick_test = 100   # number of examples to use for quick tests (every 1000 examples)

# ---------------------------------------------------------------
"""                 Simulation parameters                     """
# ---------------------------------------------------------------

use_rand_phase_lengths  = True  # use random phase lengths (chosen from Wald distribution)
use_conductances        = True  # use conductances between dendrites and soma
use_broadcast           = True  # use broadcast (ie. feedback to all layers comes from output layer)
use_spiking_feedback    = True  # use spiking feedback
use_spiking_feedforward = True  # use spiking feedforward input

use_symmetric_weights   = False # enforce symmetric weights
noisy_symmetric_weights = False # add noise to symmetric weights

use_sparse_feedback     = False # use sparse feedback weights
update_backward_weights = False # update backward weights
use_backprop            = False # use error backpropagation
use_apical_conductance  = False # use attenuated conductance from apical dendrite to soma
use_weight_optimization = False # attempt to optimize initial weights (better without currently)

record_backprop_angle   = True  # record angle b/w hidden layer error signals and backprop-generated error signals
record_loss             = True  # record final layer loss during training

# --- Jacobian testing --- #
record_eigvals          = False # record maximum eigenvalues for Jacobians
record_matrices         = False # record Jacobian product & weight product matrices (huge arrays for long simulations -- careful)
plot_eigvals            = False # dynamically plot maximum eigenvalues for Jacobians

default_simulations_folder = 'Simulations/' # folder in which to save simulations (edit accordingly)
weight_cmap                = 'bone'         # color map to use for weight plotting

dt  = 1.0         # time step (ms)
mem = int(15/dt)  # spike memory (time steps) - used to limit PSP integration of past spikes (for performance)

l_f_phase      = int(50/dt)  # length of forward phase (time steps)
l_t_phase      = int(50/dt)  # length of target phase (time steps)
l_f_phase_test = int(250/dt) # length of forward phase for tests (time steps)
settle_dur     = int(30/dt)  # duration to wait before starting to accumulate averages (time steps)

if use_rand_phase_lengths:
    min_l_f_phase = l_f_phase
    min_l_t_phase = l_t_phase

phi_max = 0.2*dt # maximum spike rate (spikes per time step)

# kernel parameters
tau_s = 3.0  # synaptic time constant
tau_L = 10.0 # leak time constant

# conductance parameters
g_B = 0.6                                   # basal conductance
g_A = 0.05 if use_apical_conductance else 0 # apical conductance
g_L = 1.0/tau_L                             # leak conductance
g_D = g_B                                   # dendritic conductance in output layer

if use_conductances:
    E_E = 8  # excitation reversal potential
    E_I = -8 # inhibition reversal potential

# steady state constants
k_B = g_B/(g_L + g_B + g_A)
k_D = g_D/(g_L + g_D)
k_I = 1.0/(g_L + g_D)

# weight update constants
P_hidden = 20.0/phi_max      # hidden layer error signal scaling factor
P_final  = 20.0/(phi_max**2) # final layer error signal scaling factor

# ---------------------------------------------------------------
"""                     Functions                             """
# ---------------------------------------------------------------

# --- activation function --- #

# spike rate equation
def phi(x):
    return phi_max/(1.0 + np.exp(-x))

def deriv_phi(x):
    return phi_max*np.exp(x)/(1.0 + np.exp(x))**2

# nonlinearity at apical dendrite
def alpha(x):
    return 1.0/(1.0 + np.exp(-x))

def deriv_alpha(x):
    return np.exp(x)/(1.0 + np.exp(x))**2

# --- kernel function --- #

def kappa(x):
    return (np.exp(-x/tau_L) - np.exp(-x/tau_s))/(tau_L - tau_s)

def get_kappas(n=mem):
    return np.array([kappa(i+1) for i in xrange(n)])

kappas = np.flipud(get_kappas(mem))[:, np.newaxis] # initialize kappas array

# ---------------------------------------------------------------
"""                     Network class                         """
# ---------------------------------------------------------------

class Network:
    def __init__(self, n):
        if type(n) == int:
            n = (n,)

        self.n = n           # layer sizes - eg. (500, 100, 10)
        self.M = len(self.n) # number of layers

        self.n_neurons_per_category = int(self.n[-1]/10)

        # load MNIST
        self.x_train, self.x_test, self.t_train, self.t_test = load_MNIST()

        self.n_in  = self.x_train.shape[0] # input size
        self.n_out = self.n[-1]            # output size

        self.x_hist = None # history of input spikes

        self.last_epoch = None # last epoch of simulation

        print("Creating network with {} layers.".format(self.M))
        print("--------------------------------")

        self.init_weights()
        self.init_layers()

    def init_weights(self):
        if use_weight_optimization:
            # initial weight optimization parameters
            V_avg = 3                  # desired average of dendritic potential
            V_sd  = 3                  # desired standard deviation of dendritic potential
            b_avg = 0.8                # desired average of bias
            b_sd  = 0.001              # desired standard deviation of bias
            nu    = phi_max*0.25       # slope of linear region of activation function
            V_sm  = V_sd**2 + V_avg**2 # second moment of dendritic potential

        # initialize lists of weight matrices & bias vectors
        self.W, self.b, self.Y, self.c = ([0]*self.M for _ in xrange(4))

        if use_sparse_feedback:
            # initialize list of indices of zeroed-out weights
            self.Y_dropout_indices = [0]*(self.M-1)

        # create dummy feedback weights for output layer (makes for loops easier)
        self.Y[self.M-1] = np.eye(self.n[self.M-1])

        for m in xrange(self.M-1, -1, -1):
            # get number of units in the layer below
            if m != 0:
                N = self.n[m-1]
            else:
                N = self.n_in

            # generate forward weights & biases
            if use_weight_optimization:
                # calculate weight variables needed to get desired average & strandard deviations of somatic potentials
                W_avg = (V_avg - b_avg)/(nu*N*V_avg)
                W_sm = (V_sm + (nu**2)*(N - N**2)*(W_avg**2)*(V_avg**2) - 2*N*nu*b_avg*V_avg*W_avg - (b_avg**2))/(N*(nu**2)*V_sm)
                W_sd = np.sqrt(W_sm - W_avg**2)
            
                self.W[m] = W_avg + 3.465*W_sd*(np.random.uniform(size=(self.n[m], N)) - 0.5)
                self.b[m] = b_avg + 3.465*b_sd*(np.random.uniform(size=(self.n[m], 1)) - 0.5)
            else:
                self.W[m] = 0.1*(np.random.uniform(size=(self.n[m], N)) - 0.5)
                self.b[m] = 1.0*(np.random.uniform(size=(self.n[m], 1)) - 0.5)

            # generate backward weights
            if m != 0:
                if use_broadcast:
                    if use_weight_optimization:
                        self.Y[m-1] = np.dot(3.465*W_sd*(np.random.uniform(size=(N, self.n[m])) - 0.5), self.Y[m])
                        self.c[m-1] = np.dot(self.Y[m-1], 3.465*W_sd*(np.random.uniform(size=(self.n[-1], 1)) - 0.5))
                    else:
                        self.Y[m-1] = (np.random.uniform(size=(N, self.n[-1])) - 0.5)
                        self.c[m-1] = (np.random.uniform(size=(N, 1)) - 0.5)
                else:
                    if use_weight_optimization:
                         self.Y[m-1] = W_avg + 3.465*W_sd*(np.random.uniform(size=(N, self.n[m])) - 0.5)
                         self.c[m-1] = W_avg + 3.465*W_sd*(np.random.uniform(size=(self.n[m], 1)) - 0.5)
                    else:
                        self.Y[m-1] = (np.random.uniform(size=(N, self.n[m])) - 0.5)
                        self.c[m-1] = (np.random.uniform(size=(self.n[m], 1)) - 0.5)

            print("Layer {0} -- {1} units.".format(m, self.n[m]))
            print("\tW_avg: {0:.6f},\tW_sd: {1:.6f},\n".format(np.mean(self.W[m]), np.std(self.W[m]))
                + "\tb_avg: {0:.6f},\tb_sd: {1:.6f},\n".format(np.mean(self.b[m]), np.std(self.b[m]))
                + "\tY_avg: {0:.6f},\tY_sd: {1:.6f}.".format(np.mean(self.Y[m]), np.std(self.Y[m])))
        print("--------------------------------\n")

        if use_symmetric_weights:
            # enforce symmetric weights
            self.make_weights_symmetric()

        if use_sparse_feedback:
            # randomly zero out 80% of weights, increase magnitude of surviving weights to keep desired average voltages
            for m in xrange(self.M-1):
                self.Y_dropout_indices[m] = np.random.choice(len(self.Y[m].ravel()), int(0.8*len(self.Y[m].ravel())), False)
                self.Y[m].ravel()[self.Y_dropout_indices[m]] = 0
                self.Y[m] *= 5

    def make_weights_symmetric(self):
        if use_broadcast:
            for m in xrange(self.M-2, -1, -1):
                # make a copy if we're altering the feedback weights after
                if use_sparse_feedback:
                    W_above = self.W[m+1].T.copy()
                else:
                    W_above = self.W[m+1].T

                if m == self.M - 2:
                    # for final hidden layer, use feedforward weights of output layer
                    if noisy_symmetric_weights:
                        self.Y[m] = W_above + np.random.normal(0, 0.05, size=W_above.shape)
                    else:
                        self.Y[m] = W_above
                else:
                    # for other hidden layers, use profuct of all feedforward weights downstream
                    if noisy_symmetric_weights:
                        self.Y[m] = np.dot(W_above + np.random.normal(0, 0.05, size=W_above.shape), self.Y[m+1])
                    else:
                        self.Y[m] = np.dot(W_above, self.Y[m+1])
        else:
            for m in xrange(self.M-2, -1, -1):
                # make a copy if we're altering the feedback weights after
                if use_sparse_feedback:
                    W_above = self.W[m+1].T.copy()
                else:
                    W_above = self.W[m+1].T

                # use feedforward weights of the layer downstream
                if noisy_symmetric_weights:
                    self.Y[m] = W_above + np.random.normal(0, 0.05)
                else:
                    self.Y[m] = W_above

    def init_layers(self):
        # initialize layers list
        self.l = []

        # create all layers
        if self.M == 1:
            self.l.append(finalLayer(net=self, m=-1, f_input_size=self.n_in))
        else:
            self.l.append(hiddenLayer(net=self, m=0, f_input_size=self.n_in, b_input_size=self.n[1]))
            for m in xrange(1, self.M-1):
                self.l.append(hiddenLayer(net=self, m=m, f_input_size=self.n[m-1], b_input_size=self.n[m+1]))
            self.l.append(finalLayer(net=self, m=self.M-1, f_input_size=self.n[-2]))

    def out_f(self, training=False, calc_averages=True):
        # do a forward pass through the network
        if use_spiking_feedforward:
            x = self.x_hist
        else:
            x = self.x

        if self.M == 1:
            self.l[0].out_f(x, None, calc_averages=calc_averages)
        else:
            if use_broadcast:
                if use_spiking_feedback:
                    self.l[0].out_f(x, self.l[-1].S_hist, calc_averages=calc_averages)

                    for m in xrange(1, self.M-1):
                        if use_spiking_feedforward:
                            self.l[m].out_f(self.l[m-1].S_hist, self.l[-1].S_hist, calc_averages=calc_averages)
                        else:
                            self.l[m].out_f(self.l[m-1].phi_C, self.l[-1].S_hist, calc_averages=calc_averages)

                    if use_spiking_feedforward:
                        self.l[-1].out_f(self.l[-2].S_hist, None, calc_averages=calc_averages)
                    else:
                        self.l[-1].out_f(self.l[-2].phi_C, None, calc_averages=calc_averages)
                else:
                    self.l[0].out_f(x, self.l[-1].phi_C, calc_averages=calc_averages)

                    for m in xrange(1, self.M-1):
                        if use_spiking_feedforward:
                            self.l[m].out_f(self.l[m-1].S_hist, self.l[-1].phi_C, calc_averages=calc_averages)
                        else:
                            self.l[m].out_f(self.l[m-1].phi_C, self.l[-1].phi_C, calc_averages=calc_averages)

                    if use_spiking_feedforward:
                        self.l[-1].out_f(self.l[-2].S_hist, None, calc_averages=calc_averages)
                    else:
                        self.l[-1].out_f(self.l[-2].phi_C, None, calc_averages=calc_averages)
            else:
                if use_spiking_feedback:
                    self.l[0].out_f(x, self.l[1].S_hist, calc_averages=calc_averages)

                    for m in xrange(1, self.M-1):
                        if use_spiking_feedforward:
                            self.l[m].out_f(self.l[m-1].S_hist, self.l[m+1].S_hist, calc_averages=calc_averages)
                        else:
                            self.l[m].out_f(self.l[m-1].phi_C, self.l[m+1].S_hist, calc_averages=calc_averages)

                    if use_spiking_feedforward:
                        self.l[-1].out_f(self.l[-2].S_hist, None, calc_averages=calc_averages)
                    else:
                        self.l[-1].out_f(self.l[-2].phi_C, None, calc_averages=calc_averages)
                else:
                    self.l[0].out_f(x, self.l[1].phi_C, calc_averages=calc_averages)

                    for m in xrange(1, self.M-1):
                        if use_spiking_feedforward:
                            self.l[m].out_f(self.l[m-1].S_hist, self.l[m+1].phi_C, calc_averages=calc_averages)
                        else:
                            self.l[m].out_f(self.l[m-1].phi_C, self.l[m+1].phi_C, calc_averages=calc_averages)

                    if use_spiking_feedforward:
                        self.l[-1].out_f(self.l[-2].S_hist, None, calc_averages=calc_averages)
                    else:
                        self.l[-1].out_f(self.l[-2].phi_C, None, calc_averages=calc_averages)

    def out_t(self, training=False, calc_averages=True):
        # same as forward pass, but with a target introduced at the top layer
        if use_spiking_feedforward:
            x = self.x_hist
        else:
            x = self.x

        if self.M == 1:
            self.l[0].out_t(x, self.t, calc_averages=calc_averages)
        else:
            if use_broadcast:
                if use_spiking_feedback:
                    self.l[0].out_t(x, self.l[-1].S_hist, calc_averages=calc_averages)

                    for m in xrange(1, self.M-1):
                        if use_spiking_feedforward:
                            self.l[m].out_t(self.l[m-1].S_hist, self.l[-1].S_hist, calc_averages=calc_averages)
                        else:
                            self.l[m].out_t(self.l[m-1].phi_C, self.l[-1].S_hist, calc_averages=calc_averages)

                    if use_spiking_feedforward:
                        self.l[-1].out_t(self.l[-2].S_hist, self.t, calc_averages=calc_averages)
                    else:
                        self.l[-1].out_t(self.l[-2].phi_C, self.t, calc_averages=calc_averages)
                else:
                    self.l[0].out_t(x, self.l[-1].phi_C, calc_averages=calc_averages)

                    for m in xrange(1, self.M-1):
                        if use_spiking_feedforward:
                            self.l[m].out_t(self.l[m-1].S_hist, self.l[-1].phi_C, calc_averages=calc_averages)
                        else:
                            self.l[m].out_t(self.l[m-1].phi_C, self.l[-1].phi_C, calc_averages=calc_averages)

                    if use_spiking_feedforward:
                        self.l[-1].out_t(self.l[-2].S_hist, self.t, calc_averages=calc_averages)
                    else:
                        self.l[-1].out_t(self.l[-2].phi_C, self.t, calc_averages=calc_averages)
            else:
                if use_spiking_feedback:
                    self.l[0].out_t(x, self.l[1].S_hist, calc_averages=calc_averages)

                    for m in xrange(1, self.M-1):
                        if use_spiking_feedforward:
                            self.l[m].out_t(self.l[m-1].S_hist, self.l[m+1].S_hist, calc_averages=calc_averages)
                        else:
                            self.l[m].out_t(self.l[m-1].phi_C, self.l[m+1].S_hist, calc_averages=calc_averages)

                    if use_spiking_feedforward:
                        self.l[-1].out_t(self.l[-2].S_hist, self.t, calc_averages=calc_averages)
                    else:
                        self.l[-1].out_t(self.l[-2].phi_C, self.t, calc_averages=calc_averages)
                else:
                    self.l[0].out_t(x, self.l[1].phi_C, calc_averages=calc_averages)

                    for m in xrange(1, self.M-1):
                        if use_spiking_feedforward:
                            self.l[m].out_t(self.l[m-1].S_hist, self.l[m+1].phi_C, calc_averages=calc_averages)
                        else:
                            self.l[m].out_t(self.l[m-1].phi_C, self.l[m+1].phi_C, calc_averages=calc_averages)

                    if use_spiking_feedforward:
                        self.l[-1].out_t(self.l[-2].S_hist, self.t, calc_averages=calc_averages)
                    else:
                        self.l[-1].out_t(self.l[-2].phi_C, self.t, calc_averages=calc_averages)

    def f_phase(self, x, t, training=False, record_voltages=False):
        if record_voltages:
            # initialize voltage arrays
            self.A_hists = [ np.zeros((l_f_phase, self.l[m].size)) for m in xrange(self.M-1) ]
            self.B_hists = [ np.zeros((l_f_phase, self.l[m].size)) for m in xrange(self.M) ]
            self.C_hists = [ np.zeros((l_f_phase, self.l[m].size)) for m in xrange(self.M) ]

        for time in xrange(l_f_phase):
            # update input spike history
            self.x_hist = np.roll(self.x_hist, -1, axis=-1)
            self.x_hist[:, -1] = np.random.poisson(x[:, 0])

            # only calculate averages if the settle duration has passed
            calc_averages = time >= settle_dur

            # do a forward pass
            self.out_f(training=training, calc_averages=calc_averages)

            if record_voltages:
                # record voltages for this timestep
                for m in xrange(self.M):
                    if m != self.M-1:
                        self.A_hists[m][time, :] = self.l[m].A[:, 0]
                    self.B_hists[m][time, :] = self.l[m].B[:, 0]
                    self.C_hists[m][time, :] = self.l[m].C[:, 0]

        # calculate averages
        for m in xrange(self.M):
            self.l[m].average_C_f     /= l_f_phase - settle_dur
            self.l[m].average_PSP_B_f /= l_f_phase - settle_dur

            if m == self.M-1:
                self.l[m].average_phi_C_f       /= l_f_phase - settle_dur
            else:
                self.l[m].average_A_f /= l_f_phase - settle_dur
                self.l[m].average_phi_C_f /= l_f_phase - settle_dur
                if update_backward_weights:
                    self.l[m].average_PSP_A_f /= l_f_phase - settle_dur

        if record_voltages and self.simulation_path:
            # append voltages to files
            for m in xrange(self.M):
                if m != self.M-1:
                    with open(os.path.join(self.simulation_path, 'A_hist_{}.csv'.format(m)), 'a') as A_hist_file:
                        np.savetxt(A_hist_file, self.A_hists[m])
                with open(os.path.join(self.simulation_path, 'B_hist_{}.csv'.format(m)), 'a') as B_hist_file:
                    np.savetxt(B_hist_file, self.B_hists[m])
                with open(os.path.join(self.simulation_path, 'C_hist_{}.csv'.format(m)), 'a') as C_hist_file:
                    np.savetxt(C_hist_file, self.C_hists[m])

    def t_phase(self, x, t, training=False, record_voltages=False, upd_b_weights=False):
        if record_voltages:
            # initialize voltage arrays
            self.A_hists = [ np.zeros((l_t_phase, self.l[m].size)) for m in xrange(self.M-1)]
            self.B_hists = [ np.zeros((l_t_phase, self.l[m].size)) for m in xrange(self.M)]
            self.C_hists = [ np.zeros((l_t_phase, self.l[m].size)) for m in xrange(self.M)]

        # update target
        self.t = t

        for time in xrange(l_t_phase):
            # update input history
            self.x_hist = np.roll(self.x_hist, -1, axis=-1)
            self.x_hist[:, -1] = np.random.poisson(x[:, 0])

            # only calculate averages if the settle duration has passed
            calc_averages = time >= settle_dur

            # do a target pass
            self.out_t(training=training, calc_averages=calc_averages)

            if record_voltages:
                # record voltages for this timestep
                for m in xrange(self.M):
                    if m != self.M-1:
                        self.A_hists[m][time, :] = self.l[m].A[:, 0]
                    self.B_hists[m][time, :] = self.l[m].B[:, 0]
                    self.C_hists[m][time, :] = self.l[m].C[:, 0]

        if record_eigvals:
            self.J_beta  = np.multiply(deriv_phi(self.l[-1].average_C_f), k_D*self.W[-1])
            self.J_gamma = np.multiply(deriv_alpha(np.dot(self.Y[-2], self.l[-1].average_phi_C_f) + self.c[-2]), self.Y[-2])

        if record_loss:
            self.loss = ((self.l[-1].average_phi_C_t - phi(self.l[-1].average_C_f)) ** 2).mean()
            
        # calculate averages
        for m in xrange(self.M-1, -1, -1):
            self.l[m].average_C_t     /= l_t_phase - settle_dur
            self.l[m].average_PSP_B_t /= l_t_phase - settle_dur

            if m == self.M-1:
                self.l[m].average_phi_C_t       /= l_t_phase - settle_dur
            else:
                self.l[m].average_A_t /= l_t_phase - settle_dur
                if update_backward_weights:
                    self.l[m].average_PSP_A_t /= l_t_phase - settle_dur

            # update weights
            self.l[m].update_W()

            if upd_b_weights:
                # update backward weights
                if m < self.M-1:
                    self.l[m].update_Y()

            # reset averages
            self.l[m].average_C_f     *= 0
            self.l[m].average_C_t     *= 0
            self.l[m].average_PSP_B_f *= 0
            self.l[m].average_PSP_B_t *= 0

            if m == self.M-1:
                self.l[m].average_phi_C_f *= 0
                self.l[m].average_phi_C_t *= 0
            else:
                self.l[m].average_A_f *= 0
                self.l[m].average_A_t *= 0
                self.l[m].average_phi_C_f *= 0
                if update_backward_weights:
                    self.l[m].average_PSP_A_f *= 0
                    self.l[m].average_PSP_A_t *= 0

        if use_symmetric_weights and (noisy_symmetric_weights or use_sparse_feedback):
            # make feedback weights symmetric to new feedforward weights
            self.make_weights_symmetric()

        if use_sparse_feedback and (use_symmetric_weights or update_backward_weights):
            for m in xrange(self.M-1):
                # zero out the inactive weights
                self.Y[m].ravel()[self.Y_dropout_indices[m]] = 0

                # increase magnitude of surviving weights
                self.Y[m] *= 5

        if record_voltages and self.simulation_path:
            # append voltages to files
            for m in xrange(self.M):
                if m != self.M-1:
                    with open(os.path.join(self.simulation_path, 'A_hist_{}.csv'.format(m)), 'a') as A_hist_file:
                        np.savetxt(A_hist_file, self.A_hists[m])
                with open(os.path.join(self.simulation_path, 'B_hist_{}.csv'.format(m)), 'a') as B_hist_file:
                    np.savetxt(B_hist_file, self.B_hists[m])
                with open(os.path.join(self.simulation_path, 'C_hist_{}.csv'.format(m)), 'a') as C_hist_file:
                    np.savetxt(C_hist_file, self.C_hists[m])

    def train(self, f_etas, b_etas, n_epochs, n_training_examples, save_simulation, simulations_folder=default_simulations_folder, folder_name="", exp_notes=None, record_voltages=False, last_epoch=-1):
        print("Starting training.\n")

        if self.last_epoch == None:
            # set last completed epoch
            self.last_epoch = last_epoch

        if use_rand_phase_lengths:
            # generate phase lengths for all training examples
            global l_f_phase, l_t_phase
            l_f_phases = min_l_f_phase + np.random.wald(2, 1, n_epochs*n_training_examples)
            l_t_phases = min_l_t_phase + np.random.wald(2, 1, n_epochs*n_training_examples)

        # don't record voltages if we're not saving the simulation
        record_voltages = record_voltages and save_simulation

        # initialize input spike history
        self.x_hist = np.zeros((self.n_in, mem))

        # get current date/time and create simulation directory
        if save_simulation:
            sim_start_time = datetime.datetime.now()

            if folder_name == "":
                self.simulation_path = os.path.join(simulations_folder, "{}.{}.{}-{}.{}".format(sim_start_time.year,
                                                                                 sim_start_time.month,
                                                                                 sim_start_time.day,
                                                                                 sim_start_time.hour,
                                                                                 sim_start_time.minute))
            else:
                self.simulation_path = os.path.join(simulations_folder, folder_name)
            
            # make simulation directory
            if not os.path.exists(self.simulation_path):
                os.makedirs(self.simulation_path)
            elif last_epoch < 0:
                print("Error: Simulation directory \"{}\" already exists.".format(self.simulation_path))
                return

            # copy current script to simulation directory
            filename = os.path.basename(__file__)
            if filename.endswith('pyc'):
                filename = filename[:-1]
            shutil.copyfile(filename, os.path.join(self.simulation_path, filename))

            params = {
                'n_full_test'            : n_full_test,
                'n_quick_test'           : n_quick_test,
                'use_rand_phase_lengths' : use_rand_phase_lengths,
                'use_conductances'       : use_conductances,
                'use_broadcast'          : use_broadcast,
                'use_spiking_feedback'   : use_spiking_feedback,
                'use_spiking_feedforward': use_spiking_feedforward,
                'use_symmetric_weights'  : use_symmetric_weights,
                'noisy_symmetric_weights': noisy_symmetric_weights,
                'use_sparse_feedback'    : use_sparse_feedback,
                'update_backward_weights': update_backward_weights,
                'use_backprop'           : use_backprop,
                'use_apical_conductance' : use_apical_conductance,
                'use_weight_optimization': use_weight_optimization,
                'record_backprop_angle'  : record_backprop_angle,
                'record_loss'            : record_loss,
                'record_eigvals'         : record_eigvals,
                'record_matrices'        : record_matrices,
                'plot_eigvals'           : plot_eigvals,
                'dt'                     : dt,
                'mem'                    : mem,
                'l_f_phase'              : l_f_phase,
                'l_t_phase'              : l_t_phase,
                'l_f_phase_test'         : l_f_phase_test,
                'settle_dur'             : settle_dur,
                'phi_max'                : phi_max,
                'tau_s'                  : tau_s,
                'tau_L'                  : tau_L,
                'g_B'                    : g_B,
                'g_A'                    : g_A,
                'g_L'                    : g_L,
                'g_D'                    : g_D,
                'k_B'                    : k_B,
                'k_D'                    : k_D,
                'k_I'                    : k_I,
                'P_hidden'               : P_hidden,
                'P_final'                : P_final,
                'n'                      : self.n,
                'f_etas'                 : f_etas,
                'b_etas'                 : b_etas,
                'n_training_examples'    : n_training_examples,
                'n_epochs'               : n_epochs
            }

            # save simulation params
            if self.last_epoch < 0:
                with open(os.path.join(self.simulation_path, 'simulation.txt'), 'w') as simulation_file:
                    print("Simulation done on {}.{}.{}-{}.{}.".format(sim_start_time.year,
                                                                     sim_start_time.month,
                                                                     sim_start_time.day,
                                                                     sim_start_time.hour,
                                                                     sim_start_time.minute), file=simulation_file)
                    if exp_notes:
                        print(exp_notes, file=simulation_file)
                    print("Start time: {}".format(sim_start_time), file=simulation_file)
                    print("-----------------------------", file=simulation_file)
                    for key, value in sorted(params.items()):
                        line = '{}: {}'.format(key, value)
                        print(line, file=simulation_file)

                with open(os.path.join(self.simulation_path, 'simulation.json'), 'w') as simulation_file:
                    simulation_file.write(json.dumps(params))
            else:
                # load previously saved recording arrays
                self.prev_full_test_errs   = np.load(os.path.join(self.simulation_path, "full_test_errors.npy"))
                self.prev_quick_test_errs  = np.load(os.path.join(self.simulation_path, "quick_test_errors.npy"))

                if record_backprop_angle:
                    self.prev_bp_angles = np.load(os.path.join(self.simulation_path, "bp_angles.npy"))

                if record_loss:
                    self.prev_losses = np.load(os.path.join(self.simulation_path, "final_layer_loss.npy"))

                if record_eigvals:
                    self.prev_max_jacobian_eigvals   = np.load(os.path.join(self.simulation_path, "max_jacobian_eigvals.npy"))
                    self.prev_max_weight_eigvals     = np.load(os.path.join(self.simulation_path, "max_weight_eigvals.npy"))
                    if record_matrices:
                        self.prev_jacobian_prod_matrices = np.load(os.path.join(self.simulation_path, "jacobian_prod_matrices.npy"))
                        self.prev_weight_prod_matrices   = np.load(os.path.join(self.simulation_path, "weight_prod_matrices.npy"))

        # set learning rate instance variables
        self.f_etas = f_etas
        self.b_etas = b_etas

        if save_simulation and self.last_epoch < 0:
            # save initial weights
            self.save_weights(self.simulation_path, prefix='initial_')

        if self.last_epoch < 0:
            # initialize full test error recording array
            self.full_test_errs  = np.zeros(n_epochs + 1)

            # initialize quick test error recording array
            self.quick_test_errs = np.zeros(n_epochs*int(n_training_examples/1000.0) + 1)
        else:
            self.full_test_errs  = np.zeros(n_epochs)
            self.quick_test_errs = np.zeros(n_epochs*int(n_training_examples/1000.0))

        if record_loss:
            self.losses = np.zeros(n_epochs*n_training_examples)

        if record_eigvals:
            # initialize arrays for Jacobian testing
            self.max_jacobian_eigvals = np.zeros(n_epochs*n_training_examples)
            if record_matrices:
                self.jacobian_prod_matrices = np.zeros((n_epochs*n_training_examples, self.n[-1], self.n[-1]))

            if self.last_epoch < 0:
                self.max_weight_eigvals = np.zeros(n_epochs*n_training_examples + 1)
                if record_matrices:
                    self.weight_prod_matrices = np.zeros((n_epochs*n_training_examples + 1, self.n[-1], self.n[-1]))
            else:
                self.max_weight_eigvals = np.zeros(n_epochs*n_training_examples)
                if record_matrices:
                    self.weight_prod_matrices = np.zeros((n_epochs*n_training_examples, self.n[-1], self.n[-1]))

            # create identity matrix
            I = np.eye(self.n[-1])

            # get max eigenvalues for weights
            U = np.dot(self.W[-1], self.Y[-2])
            p = np.dot((I - U).T, I - U)

            if self.last_epoch < 0:
                if record_matrices:
                    self.weight_prod_matrices[0] = U
                self.max_weight_eigvals[0] = np.amax(np.real(np.linalg.eigvals(p)))

        if record_backprop_angle:
            # initialize backprop angles recording array
            if self.M > 1:
                self.bp_angles = np.zeros(n_epochs*n_training_examples)

        if self.last_epoch < 0:
            # do an initial weight test
            print("Start of epoch {}.".format(self.last_epoch + 1))

            # set start time
            start_time = time.time()

            test_err = self.test_weights(n_test=n_full_test)

            # get end time & elapsed time
            end_time = time.time()
            time_elapsed = end_time - start_time

            sys.stdout.write("\x1b[2K\rFE: {0:05.2f}%. T: {1:.3f}s.\n\n".format(test_err, time_elapsed))

            self.full_test_errs[0] = test_err

            if save_simulation:
                # save full test error
                np.save(os.path.join(self.simulation_path, "full_test_errors.npy"), self.full_test_errs)

                with open(os.path.join(self.simulation_path, "full_test_errors.txt"), 'a') as test_err_file:
                    line = "%.10f" % test_err
                    print(line, file=test_err_file)

            self.quick_test_errs[0] = test_err

            if save_simulation:
                # save quick test error
                np.save(os.path.join(self.simulation_path, "quick_test_errors.npy"), self.quick_test_errs)

                with open(os.path.join(self.simulation_path, "quick_test_errors.txt"), 'a') as test_err_file:
                    line = "%.10f" % test_err
                    print(line, file=test_err_file)
        else:
            # do an initial weight test
            print("Start of epoch {}.\n".format(self.last_epoch + 1))

        # initialize input spike history
        self.x_hist   = np.zeros((self.n_in, mem))

        # start time used for timing how long each 1000 examples take
        start_time = None

        if record_eigvals and plot_eigvals:
            plt.close("all")
            fig = plt.figure(figsize=(13, 8))
            ax1 = fig.add_subplot(311)
            ax2 = fig.add_subplot(321)
            ax3 = fig.add_subplot(312)
            plt.show(block=False)

        for k in xrange(n_epochs):
            # shuffle the training data
            self.x_train, self.t_train = shuffle_arrays(self.x_train, self.t_train)

            for n in xrange(n_training_examples):
                # set start time
                if start_time == None:
                    start_time = time.time()

                if use_rand_phase_lengths:
                    l_f_phase = int(l_f_phases[k*n_training_examples + n])
                    l_t_phase = int(l_t_phases[k*n_training_examples + n])

                # print every 100 examples
                if (n+1) % 100 == 0:
                    # print("Epoch {0}, example {1:05d}/{2}.".format(self.last_epoch + 1 + k, n+1, n_training_examples))

                    sys.stdout.write("\x1b[2K\rEpoch {0}, example {1}/{2}.".format(self.last_epoch + 1 + k, n+1, n_training_examples))
                    sys.stdout.flush()

                # get training example data
                self.x = self.x_train[:, n][:, np.newaxis]
                self.t = self.t_train[:, n][:, np.newaxis]

                if record_voltages:
                    # initialize voltage arrays
                    self.A_hists     = [ np.zeros((l_f_phase, self.l[m].size)) for m in xrange(self.M-1)]
                    self.B_hists     = [ np.zeros((l_f_phase, self.l[m].size)) for m in xrange(self.M)]
                    self.C_hists     = [ np.zeros((l_f_phase, self.l[m].size)) for m in xrange(self.M)]

                # do forward & target phases
                self.f_phase(self.x, None, training=True, record_voltages=record_voltages)
                self.t_phase(self.x, self.t.repeat(self.n_neurons_per_category, axis=0), training=True, record_voltages=record_voltages, upd_b_weights=update_backward_weights)

                if record_loss:
                    self.losses[k*n_training_examples + n] = self.loss

                if record_eigvals:
                    # get max eigenvalues for jacobians
                    U = np.dot(self.J_beta, self.J_gamma)
                    p = np.dot((I - U).T, I - U)
                    if record_matrices:
                        self.jacobian_prod_matrices[k*n_training_examples + n] = U
                    self.max_jacobian_eigvals[k*n_training_examples + n] = np.amax(np.linalg.eigvals(p))

                    # get max eigenvalues for weights
                    U = np.dot(k_D*self.W[-1], self.Y[-2])
                    p = np.dot((I - U).T, I - U)
                    if self.last_epoch < 0:
                        if record_matrices:
                            self.weight_prod_matrices[k*n_training_examples + n + 1] = U
                        self.max_weight_eigvals[k*n_training_examples + n + 1] = np.amax(np.linalg.eigvals(p))
                    else:
                        if record_matrices:
                            self.weight_prod_matrices[k*n_training_examples + n] = U
                        self.max_weight_eigvals[k*n_training_examples + n] = np.amax(np.linalg.eigvals(p))
                    
                    if plot_eigvals and k == 0 and n == 0:
                        # draw initial plots
                        if record_matrices:
                            A = self.jacobian_prod_matrices[0]
                            im_plot = ax1.imshow(A, interpolation='nearest', vmin=0, vmax=1)
                            fig.colorbar(im_plot, ax=ax1)
                        if record_loss:
                            loss_plot, = ax2.plot(np.arange(1), self.losses[0])
                        max_jacobian_plot, = ax3.plot(np.arange(1), self.max_jacobian_eigvals[0])
                        fig.canvas.draw()
                        fig.canvas.flush_events()

                if record_backprop_angle:
                    # get backprop angle
                    if self.M > 1:
                        bp_angle = np.arccos(np.sum(self.l[0].delta_b_bp * self.l[0].delta_b) / (np.linalg.norm(self.l[0].delta_b_bp)*np.linalg.norm(self.l[0].delta_b.T)))*180.0/np.pi
                        self.bp_angles[k*n_training_examples + n] = bp_angle

                if (n+1) % 100 == 0:
                    if record_eigvals and plot_eigvals:
                        max_inds = np.argsort(self.max_jacobian_eigvals[k*n_training_examples + n -100:k*n_training_examples + n])
                        max_ind = np.argmax(self.max_jacobian_eigvals[k*n_training_examples + n-100:k*n_training_examples + n])
                        min_ind = np.argmin(self.max_jacobian_eigvals[k*n_training_examples + n-100:k*n_training_examples + n])
                        n_small = np.sum(self.max_jacobian_eigvals[k*n_training_examples + n-100:k*n_training_examples + n] < 1)
            
                        # update plots
                        if record_matrices:
                            A = np.mean(np.array([self.jacobian_prod_matrices[k*n_training_examples + n-100:k*n_training_examples + n][i] for i in max_inds][:-10]), axis=0)
                            im_plot.set_data(A)

                        if record_loss:
                            loss_plot.set_xdata(np.arange(k*n_training_examples + n))
                            loss_plot.set_ydata(self.losses[:k*n_training_examples + n])
                            ax2.set_xlim(0, k*n_training_examples + n)
                            ax2.set_ylim(np.amin(self.losses[:k*n_training_examples + n]) - 1e-6, np.amax(self.losses[:k*n_training_examples + n]) + 1e-6)

                        max_jacobian_plot.set_xdata(np.arange(k*n_training_examples + n))
                        max_jacobian_plot.set_ydata(self.max_jacobian_eigvals[:k*n_training_examples + n])
                        ax3.set_xlim(0, k*n_training_examples + n)
                        ax3.set_ylim(np.amin(self.max_jacobian_eigvals[:k*n_training_examples + n]) - 1e-6, np.amax(self.max_jacobian_eigvals[:k*n_training_examples + n]) + 1e-6)

                        fig.canvas.draw()
                        fig.canvas.flush_events()

                if (n+1) % 1000 == 0:
                    print("")
                    if n != n_training_examples - 1:
                        # we're partway through an epoch; do a quick weight test
                        test_err = self.test_weights(n_test=n_quick_test)

                        sys.stdout.write("\x1b[2K\rQE: {0:05.2f}%. ".format(test_err))

                        if self.last_epoch < 0:
                            self.quick_test_errs[(k+1)*int(n_training_examples/1000)] = test_err
                        else:
                            self.quick_test_errs[(k+1)*int(n_training_examples/1000) - 1] = test_err

                        if save_simulation:
                            with open(os.path.join(self.simulation_path, "quick_test_errors.txt"), 'a') as test_err_file:
                                line = "%.10f" % test_err
                                print(line, file=test_err_file)
                    else:
                        # we've reached the end of an epoch; do a full weight test
                        test_err = self.test_weights(n_test=n_full_test)

                        sys.stdout.write("\x1b[2K\rFE: {0:05.2f}%. ".format(test_err))

                        if self.last_epoch < 0:
                            self.full_test_errs[k+1] = test_err
                            self.quick_test_errs[(k+1)*int(n_training_examples/1000)] = test_err
                        else:
                            self.full_test_errs[k] = test_err
                            self.quick_test_errs[(k+1)*int(n_training_examples/1000) - 1] = test_err

                        if save_simulation:
                            with open(os.path.join(self.simulation_path, "full_test_errors.txt"), 'a') as test_err_file:
                                line = "%.10f" % test_err
                                print(line, file=test_err_file)

                        # save recording arrays
                        if save_simulation:
                            print("Saving...", end="")
                            if self.last_epoch < 0:
                                quick_test_errs = self.quick_test_errs[:(k+1)*int(n_training_examples/1000)+1]
                                if n == n_training_examples - 1:
                                    full_test_errs = self.full_test_errs[:k+2]

                                if record_backprop_angle:
                                    bp_angles = self.bp_angles[:(k+1)*n_training_examples]

                                if record_loss:
                                    losses = self.losses[:(k+1)*n_training_examples]

                                if record_eigvals:
                                    max_jacobian_eigvals   = self.max_jacobian_eigvals[:(k+1)*n_training_examples]
                                    max_weight_eigvals     = self.max_weight_eigvals[:(k+1)*n_training_examples+1]
                                    if record_matrices:
                                        jacobian_prod_matrices = self.jacobian_prod_matrices[:(k+1)*n_training_examples]
                                        weight_prod_matrices   = self.weight_prod_matrices[:(k+1)*n_training_examples+1]
                            else:
                                quick_test_errs = np.concatenate([self.prev_quick_test_errs, self.quick_test_errs[:(k+1)*int(n_training_examples/1000)]], axis=0)
                                if n == n_training_examples - 1:
                                    full_test_errs = np.concatenate([self.prev_full_test_errs, self.full_test_errs[:k+1]], axis=0)

                                if record_backprop_angle:
                                    bp_angles = np.concatenate([self.prev_bp_angles, self.bp_angles[:(k+1)*n_training_examples]], axis=0)

                                if record_loss:
                                    losses = np.concatenate([self.prev_losses, self.losses[:(k+1)*n_training_examples]], axis=0)

                                if record_eigvals:
                                    max_jacobian_eigvals   = np.concatenate([self.prev_max_jacobian_eigvals, self.max_jacobian_eigvals[:(k+1)*n_training_examples]], axis=0)
                                    max_weight_eigvals     = np.concatenate([self.prev_max_weight_eigvals, self.max_weight_eigvals[:(k+1)*n_training_examples]], axis=0)
                                    if record_matrices:
                                        jacobian_prod_matrices = np.concatenate([self.prev_jacobian_prod_matrices, self.jacobian_prod_matrices[:(k+1)*n_training_examples]], axis=0)
                                        weight_prod_matrices   = np.concatenate([self.prev_weight_prod_matrices, self.weight_prod_matrices[:(k+1)*n_training_examples]], axis=0)

                            # save quick test error
                            np.save(os.path.join(self.simulation_path, "quick_test_errors.npy".format(self.last_epoch)), quick_test_errs)

                            if n == n_training_examples - 1:
                                # save test error
                                np.save(os.path.join(self.simulation_path, "full_test_errors.npy"), full_test_errs)

                                # save weights
                                self.save_weights(self.simulation_path, prefix="epoch_{}_".format(self.last_epoch + 1 + k))

                            if record_backprop_angle:
                                if self.M > 1:
                                    # save backprop angles
                                    np.save(os.path.join(self.simulation_path, "bp_angles.npy"), bp_angles)

                            if record_loss:
                                np.save(os.path.join(self.simulation_path, "final_layer_loss.npy"), losses)

                            if record_eigvals:
                                # save eigenvalues
                                np.save(os.path.join(self.simulation_path, "max_jacobian_eigvals.npy"), max_jacobian_eigvals)
                                np.save(os.path.join(self.simulation_path, "max_weight_eigvals.npy"), max_weight_eigvals)
                                if record_matrices:
                                    np.save(os.path.join(self.simulation_path, "jacobian_prod_matrices.npy"), jacobian_prod_matrices)
                                    np.save(os.path.join(self.simulation_path, "weight_prod_matrices.npy"), weight_prod_matrices)
                            
                            print("done. ", end="")

                    if record_eigvals:
                        # print the minimum max eigenvalue of (I - J_g*J_f).T * (I - J_g*J_f) from the last 1000 examples
                        print("Min max Jacobian eigval: {:.4f}. ".format(np.amin(self.max_jacobian_eigvals[max(0, k*n_training_examples + n - 999):k*n_training_examples + n + 1])), end="")
                        
                        # print the number of max eigenvalues of (I - J_g*J_f).T * (I - J_g*J_f) from the last 1000 examples that were smaller than 1
                        print("# max eigvals < 1: {}. ".format(np.sum(self.max_jacobian_eigvals[max(0, k*n_training_examples + n - 999):k*n_training_examples + n + 1] < 1)), end="")

                    # get end time & reset start time
                    end_time = time.time()
                    time_elapsed = end_time - start_time
                    print("T: {0:.3f}s.\n".format(time_elapsed))
                    start_time = None

        # record end time of training
        if save_simulation:
            with open(os.path.join(self.simulation_path, 'simulation.txt'), 'a') as simulation_file:
                sim_end_time = datetime.datetime.now()
                print("-----------------------------", file=simulation_file)
                print("End time: {}".format(sim_end_time), file=simulation_file)

    def test_weights(self, n_test=n_quick_test):
        global l_f_phase

        # save old length of forward phase
        old_l_f_phase = l_f_phase

        # set new length of forward phase
        l_f_phase = l_f_phase_test

        # save old input spike history
        old_x_hist = self.x_hist

        # copy layer objects to be restored after testing
        old_l = copy.copy(self.l)

        # initialize count of correct classifications
        num_correct = 0

        # shuffle testing data
        self.x_test, self.t_test = shuffle_arrays(self.x_test, self.t_test)

        for n in xrange(n_test):
            # clear all layer variables
            for m in xrange(self.M):
                self.l[m].clear_vars()

            # clear input spike history
            self.x_hist  = np.zeros((self.n_in, mem))

            # get testing example data
            self.x = self.x_test[:, n][:, np.newaxis]
            self.t = self.t_test[:, n][:, np.newaxis]

            # do a forward phase & get the unit with maximum average somatic potential
            self.f_phase(self.x, self.t.repeat(self.n_neurons_per_category, axis=0), training=False)
            sel_num = np.argmax(np.mean(self.l[-1].average_C_f.reshape(-1, self.n_neurons_per_category), axis=-1))

            # get the target number from testing example data
            target_num = np.dot(np.arange(10), self.t)

            # increment correct classification counter if they match
            if sel_num == target_num:
                num_correct += 1

            # print every 100 testing examples
            if (n + 1) % 100  == 0:
                sys.stdout.write("\x1b[2K\rTesting example {0}/{1}. E: {2:05.2f}%.".format(n+1, n_test, (1.0 - float(num_correct)/(n+1))*100.0))
                sys.stdout.flush()

        # calculate percent error
        err_rate = (1.0 - float(num_correct)/n_test)*100.0

        # restore everything to its previous state
        self.l = old_l

        if old_x_hist is not None:
            self.x_hist = old_x_hist

        l_f_phase = old_l_f_phase

        if n_test > 100:
            sys.stdout.write("\x1b[2K\r")
            sys.stdout.flush()  

        return err_rate

    def plot_f_weights(self, normalize=False, save_path=None):
        plot_weights(self.W, normalize=normalize)

    def plot_b_weights(self, normalize=False, save_path=None):
        plot_weights(self.Y, normalize=normalize)

    def save_weights(self, path, prefix=""):
        for m in xrange(self.M):
            np.save(os.path.join(path, prefix + "f_weights_{}.npy".format(m)), self.W[m])
            np.save(os.path.join(path, prefix + "f_bias_{}.npy".format(m)), self.b[m])
            np.save(os.path.join(path, prefix + "b_weights_{}.npy".format(m)), self.Y[m])
            np.save(os.path.join(path, prefix + "b_bias_{}.npy".format(m)), self.c[m])

    def load_weights(self, path, prefix=""):
        print("Loading weights from \"{}\" with prefix \"{}\".".format(path, prefix))
        print("--------------------------------")

        for m in xrange(self.M):
            self.W[m] = np.load(os.path.join(path, prefix + "f_weights_{}.npy".format(m)))
            self.b[m] = np.load(os.path.join(path, prefix + "f_bias_{}.npy".format(m)))
            self.Y[m] = np.load(os.path.join(path, prefix + "b_weights_{}.npy".format(m)))
            # self.c[m] = np.load(os.path.join(path, prefix + "b_bias_{}.npy".format(m)))

        for m in xrange(self.M-1, -1, -1):
            print("Layer {0} -- {1} units.".format(m, self.n[m]))
            print("\tW_avg: {0:.6f},\tW_sd: {1:.6f},\n".format(np.mean(self.W[m]), np.std(self.W[m]))
                + "\tb_avg: {0:.6f},\tb_sd: {1:.6f},\n".format(np.mean(self.b[m]), np.std(self.b[m]))
                + "\tY_avg: {0:.6f},\tY_sd: {1:.6f}.".format(np.mean(self.Y[m]), np.std(self.Y[m])))
        print("--------------------------------\n")

# ---------------------------------------------------------------
"""                     Layer classes                         """
# ---------------------------------------------------------------

class Layer:
    def __init__(self, net, m):
        self.net  = net
        self.m    = m
        self.size = self.net.n[m]

class hiddenLayer(Layer):
    def __init__(self, net, m, f_input_size, b_input_size):
        Layer.__init__(self, net, m)

        self.f_input_size = f_input_size
        self.b_input_size = b_input_size

        self.A      = np.zeros((self.size, 1))
        self.B      = np.zeros((self.size, 1))
        self.C      = np.zeros((self.size, 1))
        self.phi_C  = np.zeros((self.size, 1))
        self.S_hist = np.zeros((self.size, mem), dtype=np.int8)

        self.delta_W = np.zeros(self.net.W[self.m].shape)
        self.delta_Y = np.zeros(self.net.Y[self.m].shape)
        self.delta_b = np.zeros((self.size, 1))

        self.average_C_f     = np.zeros((self.size, 1))
        self.average_C_t     = np.zeros((self.size, 1))
        self.average_A_f     = np.zeros((self.size, 1))
        self.average_A_t     = np.zeros((self.size, 1))
        self.average_phi_C_f = np.zeros((self.size, 1))
        self.average_PSP_B_f = np.zeros((self.f_input_size, 1))
        self.average_PSP_B_t = np.zeros((self.f_input_size, 1))

        if update_backward_weights:
            self.average_PSP_A_f = np.zeros((self.b_input_size, 1))
            self.average_PSP_A_t = np.zeros((self.b_input_size, 1))

    def clear_vars(self):
        self.A      *= 0
        self.B      *= 0
        self.C      *= 0
        self.phi_C  *= 0
        self.S_hist *= 0

        self.delta_W *= 0
        self.delta_Y *= 0
        self.delta_b *= 0

        self.average_C_f     *= 0
        self.average_C_t     *= 0
        self.average_A_f     *= 0
        self.average_A_t     *= 0
        self.average_phi_C_f *= 0
        self.average_PSP_B_f *= 0
        self.average_PSP_B_t *= 0

        if update_backward_weights:
            self.average_PSP_A_f *= 0
            self.average_PSP_A_t *= 0

    def update_W(self):
        if not use_backprop:
            self.E = (alpha(self.average_A_t) - alpha(self.average_A_f))*-k_B*deriv_phi(self.average_C_f)

            if record_backprop_angle:
                self.E_bp = np.dot(self.net.W[self.m+1].T, self.net.l[self.m+1].E_bp)*k_B*deriv_phi(self.average_C_f)
        else:
            self.E    = np.dot(self.net.W[self.m+1].T, self.net.l[self.m+1].E_bp)*k_B*deriv_phi(self.average_C_f)
            self.E_bp = self.E

        if record_backprop_angle:
            self.delta_b_bp = self.E_bp

        self.delta_W = np.dot(self.E, self.average_PSP_B_f.T)
        self.net.W[self.m] += -self.net.f_etas[self.m]*P_hidden*self.delta_W

        self.delta_b = self.E
        self.net.b[self.m] += -self.net.f_etas[self.m]*P_hidden*self.delta_b

    def update_Y(self):
        E_inv = (phi(self.average_C_f) - phi(self.average_A_f))*-deriv_phi(self.average_A_f)

        self.delta_Y = np.dot(E_inv, self.average_PSP_A_f.T)
        self.net.Y[self.m] += -self.net.b_etas[self.m]*self.delta_Y

    def update_A(self, b_input):
        if use_spiking_feedback:
            self.PSP_A = np.dot(b_input, kappas)
        else:
            self.PSP_A = b_input

        self.A = np.dot(self.net.Y[self.m], self.PSP_A) + self.net.c[self.m]

    def update_B(self, f_input):
        if use_spiking_feedforward:
            self.PSP_B = np.dot(f_input, kappas)
        else:
            self.PSP_B = f_input

        self.B = np.dot(self.net.W[self.m], self.PSP_B) + self.net.b[self.m]

    def update_C(self, phase):
        if use_conductances:
            if use_apical_conductance:
                self.C_dot = -g_L*self.C + g_B*(self.B - self.C) + g_A*(self.A - self.C)
            else:
                self.C_dot = -g_L*self.C + g_B*(self.B - self.C)
            self.C += self.C_dot*dt
        else:
            if phase == "forward":
                self.C = k_B*self.B
            elif phase == "target":
                self.C = k_B*self.B

        self.phi_C = phi(self.C)

    def spike(self):
        self.S_hist = np.roll(self.S_hist, -1, axis=-1)
        self.S_hist[:, -1] = np.random.poisson(np.maximum(self.phi_C[:, 0], 0))

    def out_f(self, f_input, b_input, calc_averages):
        self.update_B(f_input)
        self.update_A(b_input)
        self.update_C(phase="forward")
        self.spike()

        if calc_averages:
            self.average_C_f     += self.C
            self.average_A_f     += self.A
            self.average_phi_C_f += self.phi_C
            self.average_PSP_B_f += self.PSP_B

            if update_backward_weights:
                self.average_PSP_A_f += self.PSP_A

    def out_t(self, f_input, b_input, calc_averages):
        self.update_B(f_input)
        self.update_A(b_input)
        self.update_C(phase="target")
        self.spike()

        if calc_averages:
            self.average_C_t     += self.C
            self.average_A_t     += self.A
            self.average_PSP_B_t += self.PSP_B

            if update_backward_weights:
                self.average_PSP_A_t += self.PSP_A

"""
NOTE: In the paper, we denote the output layer's somatic & dendritic potentials
      as U and V. Here, we use C & B purely in order to simplify the code.
"""
class finalLayer(Layer):
    def __init__(self, net, m, f_input_size):
        Layer.__init__(self, net, m)

        self.f_input_size = f_input_size

        self.B      = np.zeros((self.size, 1))
        self.I      = np.zeros((self.size, 1))
        self.C      = np.zeros((self.size, 1))
        self.phi_C  = np.zeros((self.size, 1))
        self.S_hist = np.zeros((self.size, mem), dtype=np.int8)

        self.delta_W = np.zeros(self.net.W[self.m].shape)
        self.delta_b = np.zeros((self.size, 1))

        self.average_C_f     = np.zeros((self.size, 1))
        self.average_C_t     = np.zeros((self.size, 1))
        self.average_phi_C_f = np.zeros((self.size, 1))
        self.average_phi_C_t = np.zeros((self.size, 1))
        self.average_PSP_B_f = np.zeros((self.f_input_size, 1))
        self.average_PSP_B_t = np.zeros((self.f_input_size, 1))

    def clear_vars(self):
        self.B      *= 0
        self.I      *= 0
        self.C      *= 0
        self.phi_C  *= 0
        self.S_hist *= 0

        self.delta_W *= 0
        self.delta_b *= 0

        self.average_C_f     *= 0
        self.average_C_t     *= 0
        self.average_phi_C_f *= 0
        self.average_phi_C_t *= 0
        self.average_PSP_B_f *= 0
        self.average_PSP_B_t *= 0

    def update_W(self):
        self.E = (self.average_phi_C_t - phi(self.average_C_f))*-k_D*deriv_phi(self.average_C_f)

        if use_backprop or record_backprop_angle:
            self.E_bp = self.E

        self.delta_W = np.dot(self.E, self.average_PSP_B_f.T)
        self.net.W[self.m] += -self.net.f_etas[self.m]*P_final*self.delta_W

        self.delta_b = self.E
        self.net.b[self.m] += -self.net.f_etas[self.m]*P_final*self.delta_b

    def update_B(self, f_input):
        if use_spiking_feedforward:
            self.PSP_B = np.dot(f_input, kappas)
        else:
            self.PSP_B = f_input

        self.B = np.dot(self.net.W[self.m], self.PSP_B) + self.net.b[self.m]

    def update_I(self, input=None):
        if input is None:
            self.I *= 0
        else:
            if use_conductances:
                g_E = input
                g_I = -g_E + 1
                self.I = g_E*(E_E - self.C) + g_I*(E_I - self.C)
            else:
                self.I = (8*input - 4)

    def update_C(self, phase):
        if use_conductances:
            if phase == "forward":
                self.C_dot = -g_L*self.C + g_D*(self.B - self.C)
            elif phase == "target":
                self.C_dot = -g_L*self.C + g_D*(self.B - self.C) + self.I
            self.C += self.C_dot*dt
        else:
            if phase == "forward":
                self.C = k_D*self.B
            elif phase == "target":
                self.C = k_D*self.B + k_I*self.I

        self.phi_C = phi(self.C)

    def spike(self):
        self.S_hist = np.roll(self.S_hist, -1, axis=-1)
        self.S_hist[:, -1] = np.random.poisson(np.maximum(self.phi_C[:, 0], 0))

    def out_f(self, f_input, b_input, calc_averages):
        self.update_B(f_input)
        self.update_I(b_input)
        self.update_C(phase="forward")
        self.spike()

        if calc_averages:
            self.average_C_f     += self.C
            self.average_phi_C_f += self.phi_C
            self.average_PSP_B_f += self.PSP_B

    def out_t(self, f_input, b_input, calc_averages):
        self.update_B(f_input)
        self.update_I(b_input)
        self.update_C(phase="target")
        self.spike()

        if calc_averages:
            self.average_C_t     += self.C
            self.average_phi_C_t += self.phi_C
            self.average_PSP_B_t += self.PSP_B

# ---------------------------------------------------------------
"""                     Helper functions                      """
# ---------------------------------------------------------------

def load_simulation(last_epoch, folder_name, simulations_folder=default_simulations_folder):
    simulation_path = os.path.join(simulations_folder, folder_name)

    print("Loading simulation from \"{}\" @ epoch {}.\n".format(simulation_path, last_epoch))

    if not os.path.exists(simulation_path):
        print("Error: Could not find simulation folder – path does not exist.")
        return None

    # load parameters
    with open(os.path.join(simulation_path, 'simulation.json'), 'r') as simulation_file:
        params = json.loads(simulation_file.read())

    # set global parameters
    global n_full_test, n_quick_test
    global use_rand_phase_lengths, use_conductances, use_broadcast, use_spiking_feedback, use_spiking_feedforward
    global use_symmetric_weights, noisy_symmetric_weights
    global use_sparse_feedback, update_backward_weights, use_backprop, use_apical_conductance, use_weight_optimization
    global record_backprop_angle, record_loss, record_eigvals, record_matrices, plot_eigvals
    global dt, mem
    global l_f_phase, l_t_phase, l_f_phase_test, settle_dur
    global phi_max
    global tau_s, tau_L
    global g_B, g_A, g_L, g_D
    global k_B, k_D, k_I
    global P_hidden, P_final

    n_full_test             = params['n_full_test']
    n_quick_test            = params['n_quick_test']
    use_rand_phase_lengths  = params['use_rand_phase_lengths']
    use_conductances        = params['use_conductances']
    use_broadcast           = params['use_broadcast']
    use_spiking_feedback    = params['use_spiking_feedback']
    use_spiking_feedforward = params['use_spiking_feedforward']
    use_symmetric_weights   = params['use_symmetric_weights']
    use_sparse_feedback     = params['use_sparse_feedback']
    update_backward_weights = params['update_backward_weights']
    use_backprop            = params['use_backprop']
    use_apical_conductance  = params['use_apical_conductance']
    use_weight_optimization = params['use_weight_optimization']
    record_backprop_angle   = params['record_backprop_angle']
    record_loss             = params['record_loss']
    record_eigvals          = params['record_eigvals']
    record_matrices         = params['record_matrices']
    plot_eigvals            = params['plot_eigvals']
    dt                      = params['dt']
    mem                     = params['mem']
    l_f_phase               = params['l_f_phase']
    l_t_phase               = params['l_t_phase']
    l_f_phase_test          = params['l_f_phase_test']
    settle_dur              = params['settle_dur']
    phi_max                 = params['phi_max']
    tau_s                   = params['tau_s']
    tau_L                   = params['tau_L']
    g_B                     = params['g_B']
    g_A                     = params['g_A']
    g_L                     = params['g_L']
    g_D                     = params['g_D']
    k_B                     = params['k_B']
    k_D                     = params['k_D']
    k_I                     = params['k_I']
    P_hidden                = params['g_L']
    P_final                 = params['P_final']

    n                       = params['n']
    f_etas                  = params['f_etas']
    b_etas                  = params['b_etas']
    n_training_examples     = params['n_training_examples']

    # create network and load weights
    net = Network(n=n)
    net.load_weights(simulation_path, prefix="epoch_{}_".format(last_epoch))
    net.last_epoch = last_epoch

    return net, f_etas, b_etas, n_training_examples

# --- MNIST --- #

def save_MNIST(x_train, x_test, t_train, t_test, x_tune=None, t_tune=None):
    np.save("x_train", x_train)
    np.save("x_test", x_test)
    np.save("t_train", t_train)
    np.save("t_test", t_test)

def load_MNIST(n_tune=0):
    try:
        x_train = np.load("x_train.npy")
        x_test  = np.load("x_test.npy")
        t_train = np.load("t_train.npy")
        t_test  = np.load("t_test.npy")
        if n_tune != 0:
            x_tune = np.load("x_tune.npy")
            t_tune = np.load("t_tune.npy")
    except:
        print("Error: Could not find MNIST .npy files in the current directory.\nLooking for original binary files...")
        try:
            if n_tune != 0:
                x_train, x_test, x_tune, t_train, t_test, t_tune = get_MNIST(n_tune)
                save_MNIST(x_train, x_test, t_train, t_test, x_tune, t_tune)
            else:
                x_train, x_test, t_train, t_test = get_MNIST()
                save_MNIST(x_train, x_test, t_train, t_test)
        except:
            return

    if n_tune != 0:
        return phi_max*x_train, phi_max*x_test, phi_max*x_tune, t_train, t_test, t_tune
    else:
        return phi_max*x_train, phi_max*x_test, t_train, t_test

def get_MNIST(n_tune=0):
    '''
    Open original MNIST binary files (which can be obtained from
    http://yann.lecun.com/exdb/mnist/) and generate arrays of
    training, tuning & testing input & target vectors that are
    compatible with our neural network.

    The four binary files:
    
        train-images.idx3-ubyte
        train-labels.idx1-ubyte
        t10k-images.idx3-ubyte
        t10k-labels.idx1-ubyte

    are expected to be in the same directory as this script.
    '''

    import MNIST
    
    try:
        trainfeatures, trainlabels = MNIST.traindata()
        testfeatures, testlabels   = MNIST.testdata()
    except:
        print("Error: Could not find original MNIST files in the current directory.")
        return
 
    # normalize inputs
    if n_tune > 0:
        x_tune = trainfeatures[:, :n_tune]/255.0
         
    x_train = trainfeatures[:, n_tune:]/255.0
    x_test   = testfeatures/255.0
 
    n_train = trainlabels.size - n_tune
 
    # generate target vectors
    if n_tune > 0:
        t_tune = np.zeros((10, n_tune))
        for i in range(n_tune):
            t_tune[int(trainlabels[i]), i] = 1
 
    t_train = np.zeros((10, n_train))
    for i in xrange(n_train):
        t_train[int(trainlabels[n_tune + i]), i] = 1
 
    n_test = testlabels.size
    t_test = np.zeros((10, n_test))
    for i in xrange(n_test):
        t_test[int(testlabels[i]), i] = 1
 
    if n_tune > 0:
        return x_train, x_test, x_tune, t_train, t_test, t_tune
    else:
        return x_train, x_test, t_train, t_test

def shuffle_arrays(*args):
    p = np.random.permutation(args[0].shape[1])
    return (a[:, p] for a in args)

# --- Misc. --- #

def plot_weights(W_list, save_dir=None, suffix=None, normalize=False):
    '''
    Plots receptive fields given by weight matrices in W_list.

    W_list:    list of weight matrices (numpy arrays) to plot
    save_dir:  specifies a directory in which to save the plot.
    suffix:    suffix to add to the end of the filename of the plot.
    normalize: whether to normalize each receptive field.
    '''

    def prime_factors(n):
        # Get all prime factors of a number n.
        factors = []
        lastresult = n
        if n == 1: # 1 is a special case
            return [1]
        while 1:
            if lastresult == 1:
                break
            c = 2
            while 1:
                if lastresult % c == 0:
                    break
                c += 1
            factors.append(c)
            lastresult /= c
        print("Factors of %d: %s" % (n, str(factors)))
        return factors

    def find_closest_divisors(n):
        # Find divisors of a number n that are closest to its square root.
        a_max = np.floor(np.sqrt(n))
        if n % a_max == 0:
            a = a_max
            b = n/a
        else:
            p_fs = prime_factors(n)
            candidates = np.array([1])
            for i in xrange(len(p_fs)):
                f = p_fs[i]
                candidates = np.union1d(candidates, f*candidates)
                candidates[candidates > a_max] = 0
            a = candidates.max()
            b = n/a
        print("Closest divisors of %d: %s" % (n, str((int(b), int(a)))))
        return (int(a), int(b))

    plt.close('all')

    fig = plt.figure(figsize=(18, 9))

    M = len(W_list)

    n = [W.shape[0] for W in W_list]
    n_in = W_list[0].shape[-1]

    print(M, n)

    grid_specs = [0]*M
    axes = [ [0]*i for i in n ]

    max_Ws = [ np.amax(W) for W in W_list ]

    min_Ws = [ np.amin(W) for W in W_list ]

    W_sds = [ np.std(W) for W in W_list ]
    W_avgs = [ np.mean(W) for W in W_list ]

    for m in xrange(M):
        print("Layer {0} | W_avg: {1:.6f}, W_sd: {2:.6f}.".format(m, np.mean(W_list[m]), np.std(W_list[m])))

    for m in xrange(M):
        if m == 0:
            img_Bims = find_closest_divisors(n_in)
        else:
            img_Bims = grid_dims

        grid_dims = find_closest_divisors(n[m])
        grid_dims = (grid_dims[1], grid_dims[0]) # tanspose grid dimensions, to better fit the space

        grid_specs[m] = gs.GridSpec(grid_dims[0], grid_dims[1])

        for k in xrange(n[m]):
            row = k // grid_dims[1]
            col = k - row*grid_dims[1]

            axes[m][k] = fig.add_subplot(grid_specs[m][row, col])
            if normalize:
                heatmap = axes[m][k].imshow(W_list[m][k].reshape(img_Bims).T, interpolation="nearest", cmap=weight_cmap)
            else:
                heatmap = axes[m][k].imshow(W_list[m][k].reshape(img_Bims).T, interpolation="nearest", vmin=W_avgs[m] - 3.465*W_sds[m], vmax=W_avgs[m] + 3.465*W_sds[m], cmap=weight_cmap)
            axes[m][k].set_xticklabels([])
            axes[m][k].set_yticklabels([])

            axes[m][k].tick_params(axis='both',  # changes apply to the x-axis
                                   which='both', # both major and minor ticks are affected
                                   bottom='off', # ticks along the bottom edge are off
                                   top='off',    # ticks along the top edge are off
                                   left='off',   # ticks along the left edge are off
                                   right='off')  # ticks along the right edge are off

            if m == M-1 and k == 0:
                plt.colorbar(heatmap)

        grid_specs[m].update(left=float(m)/M,
                             right=(m+1.0)/M,
                             hspace=1.0/(grid_dims[0]),
                             wspace=0.05,
                             bottom=0.02,
                             top=0.98)

    if save_dir != None:
        if suffix != None:
            plt.savefig(save_dir + 'weights' + suffix + '.png')
        else:
            plt.savefig(save_dir + 'weights.png')
    else:
        plt.show()
