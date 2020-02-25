import scipy.spatial.distance
import numpy as np
from tqdm import tqdm_notebook
from sklearn.linear_model import LinearRegression, ElasticNetCV, Ridge
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.cm as cm
from yass.reader import READER
from yass.config import Config
import torch
import cudaSpline as deconv
from scipy.interpolate import splrep
from numpy.linalg import inv as inv
import os 
def full_rank_update(save_dir, update_object, batch_list, sps, tmps):
    updated_templates = update_object.get_updated_templates(batch_list, sps , tmps)
    np.save(os.path.join(save_dir, "templates.npy"), updated_templates)
    return os.path.join(save_dir, "templates.npy")

class RegressionTemplates:
    def __init__(self, reader, CONFIG, templates_path,lambda_pen = .5, 
                 num_iter = 10, num_basis_functions = 5):
        """
        standardized data is .npy 
        geom_array is .txt 
        n_chan = 385
        n_unit = 245
        len_wf = 61 length of individual waveforms
        lambda_pen penalization for the regression (= 0.5?)
        Parameters should be in the pipeline already

        """
        self.CONFIG = CONFIG
        self.reader = reader
        #self.spike_trains = np.load(spike_train_path)
        #self.spike_trains = self.spike_trains[np.where(self.spike_trains[:, 0] > 100)[0], :]
        
        self.geom_array = CONFIG.geom
        self.n_channels = CONFIG.recordings.n_channels
        self.sampling_rate = CONFIG.recordings.sampling_rate
        
        # Probably no need for this in the pipeline (number of basis functions already determined)
        self.num_basis_functions = num_basis_functions
        # Can fix model_rank to 5 (rank of the regression model)
        self.model_rank = 5
        self.num_iter = num_iter #number of iterations per batch in the regression (10?)
        # Only need one?
        
        self.lambda_pen = lambda_pen
        # Probably no need for this in the pipeline
        #self.max_time = max_time
        #self.min_time = min_time
        #self.templates_approx = np.zeros((self.n_unit,self.len_wf, self.n_channels, self.num_chunks))
        self.initialized = {unit: False for unit in range(self.n_unit)}
        self.find_shift()
        #self.coeff = self.get_bspline_coeffs(self.templates)
        
    def continuous_visible_channels(self, templates, threshold=.5, neighb_threshold=1., spatial_neighbor_dist=70):
    ## Should be in the pipeline already
        """
        inputs:
        -------
        templates: np.ndarray with shape (# time points, # channels, #units)
        geom: np.ndarray with shape (# channel, 2)
        threshold: float
            Weaker channels threshold
        neighb_threshold: float
            Strong channel threshold
        spatial_neighbor_dist: float
            neighboring channel threshold (70 for 512 channels retinal probe)
        """
        geom = self.geom_array #Replace here 
        ptps_ = templates.ptp(0)
        pdist = scipy.spatial.distance.squareform(
            scipy.spatial.distance.pdist(geom))
        vis_chan = (ptps_ >= neighb_threshold).astype(np.int32)
        neighbs = np.logical_and(
            pdist > 0,
            pdist < spatial_neighbor_dist).astype(np.int32)
        
        vis_chans = np.logical_or(
            np.logical_and(
                np.matmul(neighbs, vis_chan) > 0,
                ptps_ >= threshold),
            ptps_ >= neighb_threshold)
        return vis_chans
    def visible_channels(self):
        #Can rewrite function or class so no reshape here
        
        templates_reshaped = np.transpose(self.templates, (1, 2, 0))
        #self.templates.reshape(self.len_wf, self.n_channels, self.n_unit)
        visible_chans = self.continuous_visible_channels(
            templates_reshaped, threshold=.5, neighb_threshold = 4., spatial_neighbor_dist=70)
        return(visible_chans)
    
    def find_shift(self):
        """
        The four following functions to create the shifted templates should not be needed in pipeline
        Create an array that stores the shift needed for each channel
        """
        visible_chans = self.visible_channels()
        self.unit_shifts = np.zeros((self.n_unit, self.n_channels))
        for unit in range(self.n_unit):
            n_channels = self.n_channels
            shift_chan = np.zeros(n_channels)
            templates = self.templates
            if np.sum(visible_chans[:, unit]) > 4:
                idx_chan_vis = np.where(visible_chans[:, unit])[0][templates[unit, :, visible_chans[:, unit]].ptp(1).argsort()[::-1]]
            else:
                idx_chan_vis = self.templates[unit].ptp(0).argsort()[::-1][:5]
            locmin_maxchan = templates[unit, :, idx_chan_vis[0]].argmin()
            shift_chan[idx_chan_vis] = templates[unit, :,idx_chan_vis].argmin(1) - locmin_maxchan
            self.unit_shifts[unit] = shift_chan
        self.unit_shifts = self.unit_shifts.astype(int)
    def make_wf_shift_batch(self, unit, batch):
        shift_chan = self.unit_shifts[unit]
        unit_bool = self.spike_clusters == unit
        time_bool = np.logical_and(self.spike_times <= batch[1], self.spike_times >= batch[0])
        idx4 = np.where(np.logical_and(unit_bool, time_bool))[0]
                                   
        spikes = self.spike_times[idx4] - self.len_wf//2                    
        self.u_spikes = spikes
        wfs = np.zeros((len(idx4), self.len_wf, self.n_channels))
        
        data_start = batch[0]
        data_end = batch[1]  + 2*self.len_wf
        dat = self.reader.read_data(data_start, data_end)
        offset = batch[0]
        
        for i in range(len(idx4)):
            time = int(spikes[i])
            wf = np.zeros((self.len_wf, self.n_channels))
            for c in range(self.n_channels):
                wf[:, c] = dat[(-offset + time+shift_chan[c]):(-offset +time+ self.len_wf +shift_chan[c]), c]
            wfs[i, :, :] = wf
                                   
                                   
        return wfs, spikes
    
    
    def shift_templates_batch(self, unit, batch):
        n_unit = self.n_unit
        n_channels = self.n_channels
        num_chunks = self.num_chunks
        len_wf = self.len_wf
        #templates_reshifted = np.zeros((len_wf, n_channels, n_unit))
        
        u = unit
        wf, spikes_u = self.make_wf_shift_batch(u, batch)
        self.wf = wf
        num_spikes_u = len(spikes_u)
        templates_reshifted = wf.mean(0)
        return(templates_reshifted)

    def update_U(self, templates_unit_chunk, U, F, V, idx_chan_vis, num_chan_visible, num_spike_chunk, chunk):
        """
        The two update() functions might be faster with reshape/product instead of loops
        """
        lambda_pen = self.lambda_pen
        X = np.matmul(F, V[:, :, chunk]) #same shape as T
        if (num_spike_chunk>0):
            Y = templates_unit_chunk
            for i in idx_chan_vis[self.model_rank:]: # U = 1 for the channels of higher rank
                if (np.dot(X[:, i], X[:, i])>0):
                    if (chunk == 0):
                        U[i, chunk] = np.dot(X[:, i], Y[:, i])/np.dot(X[:, i], X[:, i])
                    else:
                        U[i, chunk] = (np.dot(X[:, i], Y[:, i])+lambda_pen*U[i, chunk-1])/ (np.dot(X[:, i], X[:, i])+ lambda_pen)
        return(U)
    


    def update_V(self, templates_unit_chunk, U, F, V, idx_chan_vis, num_spike_chunk, chunk):
        """
        TODO : Add penalized regression
        V = (lambda+XtX)^-1*XtY + lambda*(lambda+XtX)^-1 V_prev
        """
        len_wf = self.len_wf
        lambda_pen = self.lambda_pen
        num_chunks = self.num_chunks
        num_basis_functions = self.num_basis_functions
        len_wf = self.len_wf
        W = np.ones(len_wf)
        if (num_spike_chunk > 0):
            W[:] = 1/num_spike_chunk

        for i in idx_chan_vis[:self.model_rank] : 
            Y = templates_unit_chunk[:, i]
            if (chunk == 0):
                V[:, i, chunk] = LinearRegression(fit_intercept=False).fit(F, y=Y).coef_
            else:
                V[:, i, chunk] = Ridge(alpha = lambda_pen, fit_intercept=False).fit(F, Y).coef_ 
                mat_xx = np.matmul(F.transpose(), F)
                mat = np.linalg.pinv(lambda_pen * (np.eye(mat_xx.shape[0])) + mat_xx)
                V[:, i, chunk] = V[:, i, chunk] + lambda_pen*np.matmul(mat, V[:, i, chunk-1])

                
        X = np.zeros((len_wf, num_basis_functions))                
        for i in idx_chan_vis[self.model_rank:] : 
            X = F * U[i, chunk]
            Y = templates_unit_chunk[:, i]
            if (chunk == 0):
                V[:, i, chunk] = LinearRegression(fit_intercept=False).fit(X, Y, sample_weight=W).coef_ 
            else:
                V[:, i, chunk] = Ridge(alpha = lambda_pen, fit_intercept=False).fit(X, Y, sample_weight=W).coef_ 
                mat_xx = np.matmul(X.transpose(), X)
                mat = np.linalg.pinv(lambda_pen * (np.eye(mat_xx.shape[0])) + mat_xx)
                V[:, i, chunk] = V[:, i, chunk] + lambda_pen*np.matmul(mat, V[:, i, chunk-1])
        return(V)
    
    def initialize_regression_params(self, templates_unit, num_chan_vis):
        """
        Initialize F using SVD on FIRST batch (?)
        """
        num_basis_functions = self.num_basis_functions
        num_chunks = self.num_chunks
        F = np.zeros((self.len_wf, num_basis_functions))
        u_mat, s, vh = np.linalg.svd(templates_unit, full_matrices=False)
        F = u_mat[:, :num_basis_functions]
        V = np.zeros((num_basis_functions, num_chan_vis, num_chunks)) 
        
        for j in range(num_chunks):
            fill = (vh[:num_basis_functions, :].T * s[:num_basis_functions]).T
            V[:num_basis_functions, :, j]  = (vh[:num_basis_functions, :].T * s[:num_basis_functions]).T
        return V, F

    def batch_regression(self, unit, batch_times):
        
        #to keep consistency with previous code
        len_wf = self.len_wf
        num_basis_functions = self.num_basis_functions
        num_chunks = self.num_chunks
        num_iter = self.num_iter
        
        #get visible channels 
        visible_chans = self.visible_channels()
        visible_chans_unit = np.where(visible_chans[:, unit])[0]
        
        #small visible channels we just don't do tracking for
        if visible_chans_unit.shape[0] < 2:
            self.templates_approx[unit, :, visible_chans_unit] = tmps[unit, :, visible_chans_unit]
            return self.templates[unit]
        
        #set mininum and maximum limits
        min_time_chunk = batch_times[0]
        max_time_chunk = batch_times[1]
        batch = int(batch_times[0]/self.CONFIG.deconvolution.template_update_time)

        idx = np.where(self.spike_clusters==unit)[0]
        spikes_unit = np.int32(self.spike_times[idx]) - self.len_wf//2
        idx4 =np.where( np.logical_and(spikes_unit<= max_time_chunk,  spikes_unit>= min_time_chunk))[0]
        #if not enough spikes we use the previous batches spikes
        if idx4.shape[0] < 10:
            if not self.initialized[unit]:
                return self.templates[unit]
            return self.prev_templates[unit, :, :]
        idx_chan_vis = self.templates[unit, :, visible_chans[:, unit]].ptp(1).argsort()[::-1] #Needed to get top 5 channels
        templates_aligned = self.shift_templates_batch(unit, batch_times) #Not needed in the pipeline / faster if unit per unit
        self.vis = visible_chans_unit
        templates_unit = templates_aligned[:, visible_chans_unit]
        self.templates_unit = templates_unit
        
        ## NUM SPIKE FOR EACH CHUNK 
        num_chan_vis = len(visible_chans_unit)
        if num_chan_vis < num_basis_functions:
            templates_unit = np.append(templates_unit, np.zeros((len_wf,num_basis_functions - num_chan_vis)), axis = 1)
            num_chan_vis = 5
        # Initialization 
        if not self.initialized[unit]:
            self.V, self.F = self.initialize_regression_params(templates_unit, num_chan_vis)
            self.initialized[unit] = True
            self.U = np.ones((num_chan_vis,  num_chunks))
            self.templates_approx = np.zeros((self.n_unit,self.len_wf, self.n_channels))
            self.prev_templates[unit] = self.templates[unit]
       
        U = self.U
        V = self.V
        F = self.F
     
        # Online Regression 
        for j in range(num_iter):
            self.U = self.update_U(templates_unit, U, F, V, idx_chan_vis, num_chan_vis, self.u_spikes.shape[0], batch)
            self.V = self.update_V(templates_unit, U, F, V, idx_chan_vis, self.u_spikes.shape[0], batch)
        
        self.prev_templates[unit, :, :] = self.templates_approx[unit, :, :]
        self.templates_approx[unit, :, visible_chans_unit] = (np.matmul(F, V[:, :, batch])* U[:, batch]).T[:len(visible_chans_unit), :]
        
        return self.templates_approx[unit]

    def get_updated_templates(self, batch, sps, tmps):
        #load new spike trains 
        self.spike_trains = np.load(spike_train_path)
        self.spike_trains = self.spike_trains[np.where(self.spike_trains[:, 0] > 100)[0], :]
        self.spike_clusters = self.spike_trains[:, 1]
        self.spike_times = self.spike_trains[:, 0]
        #load new template information 
        self.templates = np.load(templates_path)
        self.n_unit = self.templates.shape[0]
        self.len_wf = self.templates.shape[1]

        tmps = np.zeros((self.n_unit, self.len_wf, self.n_channels))
        for unit in range(self.n_unit):
            tmps[unit, :, :] = self.batch_regression(unit, batch)
        return tmps
    
