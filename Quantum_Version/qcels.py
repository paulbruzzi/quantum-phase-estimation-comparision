""" Main routines for QCELS 

Quantum complex exponential least squares (QCELS) can be used to
estimate the ground-state energy with reduced circuit depth. 

Last revision: 11/22/2022
"""
import scipy.io as sio
import numpy as np
from copy import deepcopy
from numpy.linalg import eigh
from scipy.optimize import minimize
from scipy.special import erf
import cmath
import fejer_kernel
import fourier_filter
import generate_cdf

from qiskit import transpile
from qiskit_aer import AerSimulator
from qiskit.circuit import QuantumCircuit, QuantumRegister, ClassicalRegister
from qiskit_ibm_runtime import SamplerV2 as Sampler
from qiskit.circuit.library import UnitaryGate, QFT
from scipy.linalg import expm

ham_shift = np.pi/4

def modify_spectrum(ham):
    arr_ham = ham.toarray()
    arr_ham = arr_ham.astype(np.complex128)
    n = len(arr_ham[0])
    eigenenergies, _ = ham.eigh(subset_by_index = (n-1,n-1))
    max_eigenvalue = eigenenergies[0]
    norm_ham = (ham_shift)*arr_ham/max_eigenvalue
    return norm_ham

def initial_state_angle(p):
    return 2 * np.arccos((np.sqrt(2*p) + np.sqrt(2 * (1 - p)))/2)


def generate_QPE_distribution(spectrum,population,J):
    N = len(spectrum)
    dist = np.zeros(J)
    j_arr = 2*np.pi*np.arange(J)/J - np.pi
    for k in range(N):
        dist += population[k] * fejer_kernel.eval_Fejer_kernel(J,j_arr-spectrum[k])/J
    return dist

def create_HT_circuit(Ham, t, W = 'Re', p0 = 1, backend = AerSimulator()):
    qr_ancilla = QuantumRegister(1)
    qr_eigenstate = QuantumRegister(np.log2(Ham[0].shape[0]))
    cr = ClassicalRegister(1)
    qc = QuantumCircuit(qr_ancilla, qr_eigenstate, cr)
    qc.h(qr_ancilla)
    #qc.h(qr_eigenstate)
    qc.ry(initial_state_angle(p0), qr_eigenstate)
    mat = expm(-1j*Ham*t)
    controlled_U = UnitaryGate(mat).control(annotated="yes")
    qc.append(controlled_U, qargs = [qr_ancilla[:]] + qr_eigenstate[:] )
    # if W = Imaginary
    if W[0] == 'I': qc.sdg(qr_ancilla)
    qc.h(qr_ancilla)
    qc.measure(qr_ancilla[0],cr[0])
    # print(qc)
    trans_qc = transpile(qc, backend)
    
    return trans_qc

def create_QPE_circuit(ham, ancillas, p0 = 1, backend = AerSimulator()):
    qr_ancilla = QuantumRegister(ancillas)
    qr_eigenstate = QuantumRegister(np.log2(ham[0].shape[0]))
    cr = ClassicalRegister(ancillas)
    qc = QuantumCircuit(qr_ancilla, qr_eigenstate, cr)

    qc.h(qr_ancilla)
    #qc.h(qr_eigenstate)
    qc.ry(initial_state_angle(p0), qr_eigenstate)
    time_ev = expm(-2*np.pi*1j*ham)
    for i in range(len(qr_ancilla)):
        mat = np.linalg.matrix_power(time_ev, 2**(i))
        controlled_U = UnitaryGate(mat).control(annotated="yes")
        qc.append(controlled_U, qargs = [qr_ancilla[i]] + qr_eigenstate[:] )
    qc.append(QFT(len(qr_ancilla)).inverse(), qr_ancilla)
    qc.measure(qr_ancilla, cr)
    
    trans_qc = transpile(qc, backend)
    
    return trans_qc


def generate_QPE_sampling_ham(ham,Nsample,T, p0):
    T = np.ceil(np.log2(T))
    qr_ancilla = QuantumRegister(T)
    qr_eigenstate = QuantumRegister(np.log2(ham[0].shape[0]))
    cr = ClassicalRegister(T)
    qc = QuantumCircuit(qr_ancilla, qr_eigenstate, cr)

    qc.h(qr_ancilla)
    #qc.h(qr_eigenstate)
    qc.ry(initial_state_angle(p0), qr_eigenstate)
    time_ev = expm(-2*np.pi*1j*ham)
    for i in range(len(qr_ancilla)):
        mat = np.linalg.matrix_power(time_ev, 2**(i))
        controlled_U = UnitaryGate(mat).control(annotated="yes")
        qc.append(controlled_U, qargs = [qr_ancilla[i]] + qr_eigenstate[:] )
    qc.append(QFT(len(qr_ancilla)).inverse(), qr_ancilla)
    qc.measure(qr_ancilla, cr)
    
    aer_sim = AerSimulator()
    trans_qc = transpile(qc, aer_sim)
    depth = trans_qc.depth()
    counts = aer_sim.run(trans_qc, shots = Nsample).result().get_counts()
    
    max_num = 0
    binary_num = ''
    for key in counts:
        # print(key, counts[key])
        if (counts[key] > max_num):
            max_num = counts[key]
            binary_num = key
            
    decimal_num = -int(binary_num, 2) / (2 ** (T))
    return decimal_num, depth

def get_estimated_ground_energy_rough(d,delta,spectrum,population,Nsample,Nbatch):
    
    F_coeffs = fourier_filter.F_fourier_coeffs(d,delta)

    compute_prob_X = lambda T: generate_cdf.compute_prob_X_(T,spectrum,population)
    compute_prob_Y = lambda T: generate_cdf.compute_prob_Y_(T,spectrum,population)


    outcome_X_arr, outcome_Y_arr, J_arr = generate_cdf.sample_XY(compute_prob_X, 
                                compute_prob_Y, F_coeffs, Nsample, Nbatch) #Generate sample to calculate CDF

    total_evolution_time = np.sum(np.abs(J_arr))
    average_evolution_time = total_evolution_time/(Nsample*Nbatch)
    maxi_evolution_time=max(np.abs(J_arr[0,:]))

    Nx = 10
    Lx = np.pi/3
    ground_energy_estimate = 0.0
    count = 0
    #---"binary" search
    while Lx > delta:
        x = (2*np.arange(Nx)/Nx-1)*Lx +  ground_energy_estimate
        y_avg = generate_cdf.compute_cdf_from_XY(x, outcome_X_arr, outcome_Y_arr, J_arr, F_coeffs)#Calculate the value of CDF
        indicator_list = y_avg > (population[0]/2.05)
        ix = np.nonzero(indicator_list)[0][0]
        ground_energy_estimate = x[ix]
        Lx = Lx/2
        count += 1
    
    return ground_energy_estimate, count*total_evolution_time, maxi_evolution_time

def generate_filtered_Z_est(spectrum,population,t,x,d,delta,Nsample,Nbatch):
    
    F_coeffs = fourier_filter.F_fourier_coeffs(d,delta)
    compute_prob_X = lambda t_: generate_cdf.compute_prob_X_(t_,spectrum,population)
    compute_prob_Y = lambda t_: generate_cdf.compute_prob_Y_(t_,spectrum,population)
    #Calculate <\psi|F(H)\exp(-itH)|\psi>
    outcome_X_arr, outcome_Y_arr, J_arr = generate_cdf.sample_XY_QCELS(compute_prob_X, 
                                compute_prob_Y, F_coeffs, Nsample, Nbatch,t) #Generate samples using Hadmard test
    y_avg = generate_cdf.compute_cdf_from_XY_QCELS(x, outcome_X_arr, outcome_Y_arr, J_arr, F_coeffs) 
    total_time = np.sum(np.abs(J_arr))+t*Nsample*Nbatch
    max_time= max(np.abs(J_arr[0,:]))+t
    return y_avg, total_time, max_time


def generate_Z_theory(spectrum,population,t,Nsample):
    Re=0
    Im=0
    z=np.dot(population,np.exp(-1j*spectrum*t))
    Re_true=(1+z.real)/2
    Im_true=(1+z.imag)/2
    #Simulate Hadmard test
    for nt in range(Nsample):
        if np.random.uniform(0,1)<Re_true:
           Re+=1
    for nt2 in range(Nsample):
        if np.random.uniform(0,1)<Im_true:
           Im+=1
    Z_est = complex(2*Re/Nsample-1,2*Im/Nsample-1)
    max_time = t
    total_time = t * Nsample
    return Z_est, total_time, max_time 
    
def generate_data_sim(Ham, t, Nsample, W = 'Re', p0 = 1):
    qr_ancilla = QuantumRegister(1)
    qr_eigenstate = QuantumRegister(np.log2(Ham[0].shape[0]))
    cr = ClassicalRegister(1)
    qc = QuantumCircuit(qr_ancilla, qr_eigenstate, cr)
    qc.h(qr_ancilla)
    #qc.h(qr_eigenstate)
    qc.ry(initial_state_angle(p0), qr_eigenstate)
    mat = expm(-1j*Ham*t)
    controlled_U = UnitaryGate(mat).control(annotated="yes")
    qc.append(controlled_U, qargs = [qr_ancilla[:]] + qr_eigenstate[:] )
    # if W = Imaginary
    if W[0] == 'I': qc.sdg(qr_ancilla)
    qc.h(qr_ancilla)
    qc.measure(qr_ancilla[0],cr[0])
    # print(qc)
    aer_sim = AerSimulator()
    trans_qc = transpile(qc, aer_sim)
    depth = trans_qc.depth()
    counts = aer_sim.run(trans_qc, shots = Nsample).result().get_counts()
    # print("\t\tget counts")
    return counts, depth

def generate_HT_circuit(Backend, Ham, t, Nsample, W = 'Re', p0 = 1):
    qr_ancilla = QuantumRegister(1)
    qr_eigenstate = QuantumRegister(np.log2(Ham[0].shape[0]))
    cr = ClassicalRegister(1)
    qc = QuantumCircuit(qr_ancilla, qr_eigenstate, cr)
    qc.h(qr_ancilla)
    #qc.h(qr_eigenstate)
    qc.ry(initial_state_angle(p0), qr_eigenstate)
    mat = expm(-1j*Ham*t)
    controlled_U = UnitaryGate(mat).control(annotated="yes")
    qc.append(controlled_U, qargs = [qr_ancilla[:]] + qr_eigenstate[:] )
    # if W = Imaginary
    if W[0] == 'I': qc.sdg(qr_ancilla)
    qc.h(qr_ancilla)
    qc.measure(qr_ancilla[0],cr[0])
    trans_qc = transpile(qc, Backend)
    return trans_qc

def generate_Z_sim(Ham, t, Nsample, p0 = 1):
    # print("\tstarted Re Hadamard test")
    countsRe, depthRe = generate_data_sim(Ham, t, Nsample, W = 'Re', p0 = p0)    
    # print("\tstarted Im Hadamard test")
    countsIm, depthIm = generate_data_sim(Ham, t, Nsample, W = 'Im', p0 = p0)
    # print("\tended Hadamard tests")

    
    re_p0 = im_p0 = 0
    if countsRe.get('0') is not None:
        re_p0 = countsRe['0']/Nsample
    if countsIm.get('0') is not None:
        im_p0 = countsIm['0']/Nsample
    
    Re = 2*re_p0-1
    Im = 2*im_p0-1 

    Angle = np.arccos(Re)
    if  np.arcsin(Im)<0:
        Phase = 2*np.pi - Angle
    else:
        Phase = Angle
    
    Z_est = complex(np.cos(Phase),np.sin(Phase))
    max_time = t
    #max_time = depthRe + depthIm 
    #total_time = t * Nsample
    total_time = depthRe + depthIm 
    return Z_est, total_time, max_time

def get_Z_Re(Backend, Ham, t, Nsample, p0):
    circuitRe = generate_HT_circuit(Backend, Ham, t, Nsample, W = 'Re', p0 = p0)
    circuitIm = generate_HT_circuit(Backend, Ham, t, Nsample, W = 'Im', p0 = p0) 

    sampler = Sampler(Backend)
    results = sampler.run([circuitRe, circuitIm], shots = Nsample).result()

    countsRe = results[0].data['c2'].get_counts()
    countsIm = results[1].data['c3'].get_counts()
    
    re_p0 = im_p0 = 0
    if countsRe.get('0') is not None:
        re_p0 = countsRe['0']/Nsample
    if countsIm.get('0') is not None:
        im_p0 = countsIm['0']/Nsample
    
    print('Re: ',re_p0,'Im: ',im_p0)
    Re = 2*re_p0-1
    Im = 2*im_p0-1 

    Angle = np.arccos(Re)
    if  np.arcsin(Im)<0:
        Phase = 2*np.pi - Angle
    else:
        Phase = Angle
    
    Z_est = complex(np.cos(Phase),np.sin(Phase))
    max_time = t
    total_time = t * Nsample
    return Z_est, total_time, max_time

def generate_spectrum_population(eigenenergies, population, p):
    
    p = np.array(p)
    spectrum = eigenenergies * (ham_shift) / np.max(np.abs(eigenenergies))#normalize the spectrum
    q = population
    num_p = p.shape[0]
    q[0:num_p] = p/(1-np.sum(p))*np.sum(q[num_p:])
    return spectrum, q/np.sum(q)

def qcels_opt_fun(x, ts, Z_est):
    NT = ts.shape[0]
    Z_fit=np.zeros(NT,dtype = 'complex') # 'complex_'
    Z_fit=(x[0]+1j*x[1])*np.exp(-1j*x[2]*ts)
    return (np.linalg.norm(Z_fit-Z_est)**2/NT)

def qcels_opt(ts, Z_est, x0, bounds = None, method = 'SLSQP'):

    fun = lambda x: qcels_opt_fun(x, ts, Z_est)
    if( bounds ):
        res=minimize(fun,x0,method = 'SLSQP',bounds=bounds)
    else:
        res=minimize(fun,x0,method = 'SLSQP',bounds=bounds)

    return res

def get_tau(j, epsilon, delta, time_steps, iterations, T):
    #return 2**(j - np.ceil(np.log2(1/epsilon)))*(2**(-4))#*(delta/(time_steps*epsilon))#
    #return 2**(j)*epsilon
    return T/time_steps/(2**(iterations-j))

def qcels_largeoverlap_new(Z_est, time_steps, lambda_prior, delta, epsilon, T):
    """Multi-level QCELS for a system with a large initial overlap.

    Description: The code of using Multi-level QCELS to estimate the ground state energy for a systems with a large initial overlap

    Args: eigenvalues of the Hamiltonian: spectrum; 
    overlaps between the initial state and eigenvectors: population; 
    the depth for generating the data set: T; 
    number of data pairs(time steps): time_steps; 
    number of samples: Nsample; 
    initial guess of \lambda_0: lambda_prior

    Returns: an estimation of \lambda_0; 
    maximal evolution time T_{max}; 
    total evolution time T_{total}

    """
    iterations = len(Z_est) - 1
    #tau=delta/time_steps/epsilon
    tau = get_tau(0, epsilon, delta, time_steps, iterations, T)
    ts=tau*np.arange(time_steps)
    print("      Preprocessing", flush = True)
    #Step up and solve the optimization problem
    x0=np.array((0.5,0,lambda_prior))
    res = qcels_opt(ts, Z_est[0], x0)#Solve the optimization problem
    #Update initial guess for next iteration
    ground_coefficient_QCELS=res.x[0]
    ground_coefficient_QCELS2=res.x[1]
    ground_energy_estimate_QCELS=res.x[2]
    #Update the estimation interval
    lambda_min=ground_energy_estimate_QCELS-np.pi/(2*tau) 
    lambda_max=ground_energy_estimate_QCELS+np.pi/(2*tau) 
    for iter in range(1, iterations + 1):
        print('      Starting Iteration', "("+str(iter)+'/'+str(iterations)+")", flush = True)
        tau = get_tau(iter, epsilon, delta, time_steps, iterations, T)
        ts=tau*np.arange(time_steps)
        #Step up and solve the optimization problem
        x0=np.array((ground_coefficient_QCELS,ground_coefficient_QCELS2,ground_energy_estimate_QCELS))
        bnds=((-np.inf,np.inf),(-np.inf,np.inf),(lambda_min,lambda_max)) 
        res = qcels_opt(ts, Z_est[iter], x0, bounds=bnds)#Solve the optimization problem
        #Update initial guess for next iteration
        ground_coefficient_QCELS=res.x[0]
        ground_coefficient_QCELS2=res.x[1]
        ground_energy_estimate_QCELS=res.x[2]
        #Update the estimation interval
        lambda_min=ground_energy_estimate_QCELS-np.pi/(2*tau) 
        lambda_max=ground_energy_estimate_QCELS+np.pi/(2*tau) 
    print("      Finished Iterations", flush = True)
    return res


# method has been modified to allow computation for THEORY, SIMULATION, and REAL HARDWARE
def qcels_largeoverlap(T, NT, Nsample, lambda_prior, computation_type = 'THEORY', spectrum=[], population=[], ham = [], backend=None, p0 = 1):
    """Multi-level QCELS for a system with a large initial overlap.

    Description: The code of using Multi-level QCELS to estimate the ground state energy for a systems with a large initial overlap

    Args: eigenvalues of the Hamiltonian: spectrum; 
    overlaps between the initial state and eigenvectors: population; 
    the depth for generating the data set: T; 
    number of data pairs: NT; 
    number of samples: Nsample; 
    initial guess of \lambda_0: lambda_prior

    Returns: an estimation of \lambda_0; 
    maximal evolution time T_{max}; 
    total evolution time T_{total}

    """
    total_time_all = 0.
    max_time_all = 0.

    def get_data():
        if computation_type[0].upper() == 'T':
            result = generate_Z_theory(spectrum,population,ts[i],Nsample)
        if computation_type[0].upper() == 'S':
            result = generate_Z_sim(ham,ts[i],Nsample,p0)
        if computation_type[0].upper() == 'R':
            result = get_Z_Re(backend, ham,ts[i],Nsample, p0)
        return result
    
    if computation_type[0].upper() == 'T':
        N_level=int(np.log2(T/NT))
    if computation_type[0].upper() == 'S':
        #N_level = T
        N_level=int(np.log2(T/NT))
    
    tau=T/NT/(2**N_level)
    ts=tau*np.arange(NT)
    Z_est=np.zeros(NT,dtype = 'complex') #'complex_'
    print("      Preprocessing", flush = True)
    for i in range(NT):
        Z_est[i], total_time, max_time = get_data()
        print("        Z_est for timestep",i+1,"=", Z_est[i], flush = True)
        total_time_all += total_time
        max_time_all = max(max_time_all, max_time)
    #Step up and solve the optimization problem
    x0=np.array((0.5,0,lambda_prior))
    res = qcels_opt(ts, Z_est, x0)#Solve the optimization problem
    #Update initial guess for next iteration
    ground_coefficient_QCELS=res.x[0]
    ground_coefficient_QCELS2=res.x[1]
    ground_energy_estimate_QCELS=res.x[2]
    #Update the estimation interval
    lambda_min=ground_energy_estimate_QCELS-np.pi/(2*tau) 
    lambda_max=ground_energy_estimate_QCELS+np.pi/(2*tau) 
    for n_QCELS in range(N_level):
        print('      Starting Iteration', "("+str(n_QCELS+1)+'/'+str(N_level)+")", flush = True)
        Z_est=np.zeros(NT,dtype = 'complex') # 'complex_'
        tau=T/NT/(2**(N_level-n_QCELS-1)) #generate a sequence of \tau_j
        ts=tau*np.arange(NT)
        for i in range(NT):
            Z_est[i], total_time, max_time = get_data()
            print("        Z_est for timestep",i+1,"=", Z_est[i], flush = True)
            total_time_all += total_time
            max_time_all = max(max_time_all, max_time)
        #Step up and solve the optimization problem
        x0=np.array((ground_coefficient_QCELS,ground_coefficient_QCELS2,ground_energy_estimate_QCELS))
        bnds=((-np.inf,np.inf),(-np.inf,np.inf),(lambda_min,lambda_max)) 
        res = qcels_opt(ts, Z_est, x0, bounds=bnds)#Solve the optimization problem
        #Update initial guess for next iteration
        ground_coefficient_QCELS=res.x[0]
        ground_coefficient_QCELS2=res.x[1]
        ground_energy_estimate_QCELS=res.x[2]
        #Update the estimation interval
        lambda_min=ground_energy_estimate_QCELS-np.pi/(2*tau) 
        lambda_max=ground_energy_estimate_QCELS+np.pi/(2*tau) 
    print("      Finished Iterations", flush = True)
    return res, total_time_all, max_time_all

def qcels_smalloverlap(spectrum, population, T, NT, d, rel_gap, err_tol_rough, Nsample_rough, Nsample):
    """Multi-level QCELS with a filtered data set for a system with a small initial overlap.

    Description: The codes of using Multi-level QCELS and eigenvalue filter to estimate the ground state energy for
    a system with a small initial overlap

    Args: eigenvalues of the Hamiltonian: spectrum; 
    overlaps between the initial state and eigenvectors: population; 
    the depth for generating the data set: T; 
    number of data pairs: NT; 
    number of samples for constructing the eigenvalue filter: Nsample_rough; 
    number of samples for generating the data set: Nsample; 
    initial guess of \lambda_0: lambda_prior
    
    Returns: an estimation of \lambda_0; 
    maximal evolution time T_{max}; 
    total evolution time T_{total}

    """
    total_time_all = 0.
    max_time_all = 0.

    lambda_prior, total_time_prior, max_time_prior = get_estimated_ground_energy_rough(
            d,err_tol_rough,spectrum,population,Nsample_rough,Nbatch=1) #Get the rough estimation of the ground state energy
    x = lambda_prior + rel_gap/2 #center of the eigenvalue filter
    total_time_all += total_time_prior
    max_time_all = max(max_time_all, max_time_prior)
    
    N_level=int(np.log2(T/NT))
    Z_est=np.zeros(NT,dtype = 'complex')
    tau=T/NT/(2**N_level)
    ts=tau*np.arange(NT)
    for i in range(NT):
        Z_est[i], total_time, max_time=generate_filtered_Z_est(
                spectrum,population,ts[i],x,d,err_tol_rough,Nsample_rough,Nbatch=1)#Approximate <\psi|\exp(-itH)|\psi> using Hadmard test
        total_time_all += total_time
        max_time_all = max(max_time_all, max_time)
    #Step up and solve the optimization problem
    x0=np.array((0.5,0,lambda_prior))
    res = qcels_opt(ts, Z_est, x0)#Solve the optimization problem
    #Update initial guess for next iteration
    ground_coefficient_QCELS=res.x[0]
    ground_coefficient_QCELS2=res.x[1]
    ground_energy_estimate_QCELS=res.x[2]
    #Update the estimation interval
    lambda_min=ground_energy_estimate_QCELS-np.pi/(2*tau)
    lambda_max=ground_energy_estimate_QCELS+np.pi/(2*tau)
    #Iteration
    for n_QCELS in range(N_level):
        Z_est=np.zeros(NT,dtype = 'complex')
        tau=T/NT/(2**(N_level-n_QCELS-1))
        ts=tau*np.arange(NT)
        for i in range(NT):
            Z_est[i], total_time, max_time=generate_filtered_Z_est(
                    spectrum,population,ts[i],x,d,err_tol_rough,Nsample,Nbatch=1)#Approximate <\psi|\exp(-itH)|\psi> using Hadmard test
            total_time_all += total_time
            max_time_all = max(max_time_all, max_time)
        #Step up and solve the optimization problem
        x0=np.array((ground_coefficient_QCELS,ground_coefficient_QCELS2,ground_energy_estimate_QCELS))
        bnds=((-np.inf,np.inf),(-np.inf,np.inf),(lambda_min,lambda_max))
        res = qcels_opt(ts, Z_est, x0, bounds=bnds)#Solve the optimization problem
        #Update initial guess for next iteration
        ground_coefficient_QCELS=res.x[0]
        ground_coefficient_QCELS2=res.x[1]
        ground_energy_estimate_QCELS=res.x[2]
        #Update the estimation interval
        lambda_min=ground_energy_estimate_QCELS-np.pi/(2*tau)
        lambda_max=ground_energy_estimate_QCELS+np.pi/(2*tau)

    return ground_energy_estimate_QCELS, total_time_all, max_time_all


if __name__ == "__main__":
    import numpy as np
    import sys
    from numpy.linalg import eigh
    from matplotlib import pyplot as plt
    import matplotlib
    import tfim_1d
    import generate_cdf
    from qcels import *
    matplotlib.rcParams['font.size'] = 15
    matplotlib.rcParams['lines.markersize'] = 10

    def modify_spectrum(ham):
        arr_ham = ham.toarray()
        arr_ham = arr_ham.astype(np.complex128)
        n = len(arr_ham[0])
        eigenenergies, _ = ham.eigh(subset_by_index = (n-1,n-1))
        max_eigenvalue = eigenenergies[0]
        norm_ham = (ham_shift)*arr_ham/max_eigenvalue
        return norm_ham

    num_sites = 8
    J = 1.0
    g = 4.0

    # calculate the ground state with g = 1
    ham0 = tfim_1d.generate_ham(num_sites, J, 1.0)
    ground_state_0 = ham0.eigh(subset_by_index = (0,0))[1][:,0]

    # plot original spectrum
    ham = tfim_1d.generate_ham(num_sites, J, g)
    eigenenergies, eigenstates = ham.eigh()
    ground_state = eigenstates[:,0]
    population_raw = np.abs(np.dot(eigenstates.conj().T, ground_state_0))**2

    old_ham = ham

    # create modified spectrum
    ham = modify_spectrum(old_ham)
    eigenenergies, eigenstates = eigh(ham)
    ground_state = eigenstates[:,0]
    population = np.abs(np.dot(eigenstates.conj().T, ground_state_0))**2

    p0_array            = np.array([0.6,0.8]) # initial overlap with the first eigenvector
    N_test              = 10 # number of comparisions each trial (circuit depths)
    trials              = 10
    err_threshold       = 0.01
    T0                  = 100

    # QCELS variables
    # T_list_QCELS        = 10+T0/2*(np.arange(N_test)) # circuit depth for QCELS
    err_QCELS           = np.zeros((len(p0_array),N_test))
    cost_list_avg_QCELS = np.zeros((len(p0_array),N_test))
    rate_success_QCELS  = np.zeros((len(p0_array),N_test))
    max_T_QCELS         = np.zeros((len(p0_array),N_test))

    # QPE variables
    # T_list_QPE          = 10+T0*4*(np.arange(N_test)) # circuit depth for QPE
    err_QPE             = np.zeros((len(p0_array),N_test))
    cost_list_avg_QPE   = np.zeros((len(p0_array),N_test))
    rate_success_QPE    = np.zeros((len(p0_array),N_test))
    QPE_depth           = np.zeros(N_test)
    
        # set computation type to T (Theoretical Simulation), S (Quantum Simulation), or R (Quantum Hardware)
    computation_type = 'S'
    output_file = True

    # initialization
    with open('out.txt', 'w') as f:
        sys.stdout = f
        sys.stderr = f
        if computation_type[0].upper() == 'T':
            print("\nTHEORETICAL SIMULATION\n", flush = True)
            T_list_QCELS = 10+T0/2*(np.arange(N_test))
            T_list_QPE = 10+T0*4*(np.arange(N_test))
            data_name = "T_Sim"

        if computation_type[0].upper() == 'S':
            print("\nQUANTUM SIMULATION\n", flush = True)
            #T_list_QCELS = np.arange(N_test) + 1
            #T_list_QPE = np.arange(N_test) + 1
            T_list_QCELS = 10+T0/2*(np.arange(N_test))
            T_list_QPE = 10+T0*4*(np.arange(N_test))
            data_name = "Q_Sim"

        if computation_type[0].upper() == 'R':
            print("\nQUANTUM HARDWARE\n", flush = True)
            T_list_QCELS = 10+T0/2*(np.arange(N_test))
            T_list_QPE = 10+T0*4*(np.arange(N_test))
            # save qiskit API token for later use
            api_token = input("Enter API Token:")
            data_name = "Q_Real"

        def qcels():
            if computation_type[0].upper() == 'T':
                result = qcels_largeoverlap(T, data_pairs, Nsample, lambda_prior, computation_type = computation_type,
                                            spectrum=spectrum, population=population)
                
            if computation_type[0].upper() == 'S':
                result = qcels_largeoverlap(T, data_pairs, Nsample, lambda_prior, computation_type = computation_type, ham = ham, p0 = p0)

            if computation_type[0].upper() == 'R':
                from qiskit_ibm_runtime import QiskitRuntimeService as QRS
                service = QRS(channel = 'ibm_quantum', instance='rpi-rensselaer/research/faulsf', token = api_token)
                backend = service.backend('ibm_rensselaer')
                # backend = service.least_busy(simulator=False, operational=True)
                result = qcels_largeoverlap(T, data_pairs, Nsample, lambda_prior, computation_type = computation_type, ham = ham,
                                            backend = backend, p0 = p0)
            return result

        def QPE_Est():
            if computation_type[0].upper() == 'T':
                discrete_energies = 2*np.pi*np.arange(2*T)/(2*T) - np.pi
                dist = generate_QPE_distribution(spectrum,population,2*T) #Generate QPE samples
                samp = generate_cdf.draw_with_prob(dist,N_try_QPE) # uses dist with counts to get samp
                j_min = samp.min()
                result = discrete_energies[j_min], T_list_QPE[ix]

            if computation_type[0].upper() == 'S':
                result = generate_QPE_sampling_ham(ham, Nsample, 2*T, p0 = p0)

            if computation_type[0].upper() == 'R':
                result = generate_QPE_sampling_ham(ham, Nsample, 2*T, p0 = p0)
            return result

        if output_file:
            outfile = open("Output/"+str(data_name)+"_long.txt", 'w')

        for a1 in range(len(p0_array)):
            p0=p0_array[a1]
            n_success_QCELS= np.zeros(N_test)
            n_success_QPE= np.zeros(N_test)

            print("Testing p0 =", p0,"("+str(a1+1)+"/"+str(len(p0_array))+")", flush = True)

            if output_file: print("Testing p0 =", p0,"("+str(a1+1)+"/"+str(len(p0_array))+")", file = outfile, flush = True)

            for trial in range(trials):

                print("  Generating QCELS and QPE data", "(p0="+str(p0)+")","("+str(trial+1)+"/"+str(trials)+")", flush = True)

                spectrum, population = generate_spectrum_population(eigenenergies, population_raw, [p0])

                #------------------QCELS-----------------
                Nsample = 100 # number of samples for constructing the loss function

                for ix in range(N_test):
                    print("    Running QCELS", "("+str(ix+1)+"/"+str(N_test)+")", flush = True)

                    if output_file: print("    Running QCELS", "("+str(ix+1)+"/"+str(N_test)+")", file = outfile, flush = True)
                    T = T_list_QCELS[ix]
                    data_pairs = 5
                    lambda_prior = spectrum[0]
                    ground_energy_estimate_QCELS, cosT_depth_list_this, max_T_QCELS_this = qcels()

                    print("      Estimated ground state energy =", ground_energy_estimate_QCELS, flush = True)
                    if output_file: print("      Estimated ground state energy =", ground_energy_estimate_QCELS.x[2], file = outfile, flush = True)

                    err_this_run_QCELS = np.abs(ground_energy_estimate_QCELS.x[2] - spectrum[0])
                    err_QCELS[a1,ix] = err_QCELS[a1,ix]+np.abs(err_this_run_QCELS)
                    cost_list_avg_QCELS[a1,ix]=cost_list_avg_QCELS[a1,ix]+cosT_depth_list_this
                    max_T_QCELS[a1,ix]=max(max_T_QCELS[a1,ix],max_T_QCELS_this)

                    if np.abs(err_this_run_QCELS)<err_threshold:
                        n_success_QCELS[ix]+=1

                print("    Finished QCELS data\n", flush = True)
                if output_file: print("    Finished QCELS data\n", file = outfile, flush = True)

            # ----------------- QPE -----------------------
                # N_try_QPE = int(15*np.ceil(1.0/p0)) #number of QPE samples each time

                # for ix in range(N_test):
                #     print("    Running QPE", "("+str(ix+1)+"/"+str(N_test)+")", flush = True)
                #     if output_file: print("    Running QPE", "("+str(ix+1)+"/"+str(N_test)+")", file = outfile, flush=True)
                    
                #     T = T_list_QPE[ix]
                    
                #     ground_energy_estimate_QPE, QPEdepth = QPE_Est()

                #     print("      Estimated ground state energy =", ground_energy_estimate_QPE, flush = True)
                #     if output_file: print("      Estimated ground state energy =", ground_energy_estimate_QPE, file = outfile, flush = True)

                #     err_this_ruN_test = np.abs(ground_energy_estimate_QPE-spectrum[0])
                #     err_QPE[a1,ix] = err_QPE[a1,ix]+np.abs(err_this_ruN_test)

                #     if np.abs(err_this_ruN_test)<err_threshold:
                #         n_success_QPE[ix]+=1
                #     #cost_list_avg_QPE[a1,ix] = T*N_try_QPE
                #     cost_list_avg_QPE[a1,ix] = QPEdepth

                # print("    Finished QPE data\n", flush = True)
                # if output_file: print("    Finished QPE data\n", file = outfile, flush = True)

            rate_success_QCELS[a1,:] = n_success_QCELS[:]/trials
            # rate_success_QPE[a1,:] = n_success_QPE[:]/trials
            err_QCELS[a1,:] = err_QCELS[a1,:]/trials
            # err_QPE[a1,:] = err_QPE[a1,:]/trials
            cost_list_avg_QCELS[a1,:]=cost_list_avg_QCELS[a1,:]/trials

        # np.savez('Data/'+data_name+'_long_result_TFIM_8sites_QPE',name1=rate_success_QPE,name2=T_list_QPE,name3=cost_list_avg_QPE,name4=err_QPE)
        np.savez('Data/'+data_name+'_long_result_TFIM_8sites_QCELS',name1=rate_success_QCELS,name2=max_T_QCELS,name3=cost_list_avg_QCELS,name4=err_QCELS)
        np.savez('Data/'+data_name+'_long_TFIM_8sites_data',name1=spectrum,name2=population,name3=ground_energy_estimate_QCELS.x[0],
                name4=ground_energy_estimate_QCELS.x[1],name5=ground_energy_estimate_QCELS.x[2])

        print("Saved data to files starting with", data_name, flush = True)
        if output_file: print("Saved data to files starting with", data_name, file = outfile, flush=True)
        outfile.close()
        if output_file: print("Saved output to file ", "Output/"+str(data_name)+"_long.txt", flush = True)



        # print('QCELS')
        # print(rate_success_QCELS)
        # print('QPE')
        # print(rate_success_QPE)    
        # plt.figure(figsize=(12,10))
        # plt.plot(T_list_QCELS,err_QCELS[0,:],linestyle="-.",marker="o",label="error of QCELS p_0=0.8")
        # plt.plot(T_list_QPE,err_QPE[0,:],linestyle="-.",marker="*",label="error of QPE p_0=0.8")
        # plt.xlabel("$T_{max}$",fontsize=35)
        # plt.ylabel("error($\epsilon$)",fontsize=35)
        # plt.xscale("log")
        # plt.yscale("log")
        # plt.legend()
        # plt.figure(figsize=(12,10))
        # plt.plot(cost_list_avg_QCELS[0,:],err_QCELS[0,:],linestyle="-.",marker="o",label="error of QCELS p_0=0.8")
        # plt.plot(cost_list_avg_QPE[0,:],err_QPE[0,:],linestyle="-.",marker="*",label="error of QPE p_0=0.8")
        # plt.xlabel("$T_{total}$",fontsize=35)
        # plt.ylabel("error($\epsilon$)",fontsize=35) 
        # plt.xscale("log")
        # plt.yscale("log")
        # plt.legend(fontsize=25)
        # plt.show()


        # #
        # print("small overlap using multi-level QCELS with filtered data")
        # p0_array=np.array([0.1]) #population
        # p1_array=np.array([0.025]) #population
        # #relative population=0.8
        # T0 = 2000
        # N_test_QCELS = 10  #number of QCELS test
        # N_QPE = 10  #number of QPE test
        # T_list_QCELS = 150+T0/4*(np.arange(N_test_QCELS))
        # T_list_QPE = 150+T0*7.5*(np.arange(N_QPE))
        # err_QCELS=np.zeros((len(p0_array),len(T_list_QCELS)))
        # err_QPE=np.zeros((len(p0_array),len(T_list_QPE)))
        # cost_list_avg_QCELS = np.zeros((len(p0_array),len(T_list_QCELS)))
        # cost_list_avg_QPE = np.zeros((len(p0_array),len(T_list_QPE)))
        # rate_success_QCELS=np.zeros((len(p0_array),len(T_list_QCELS)))
        # rate_success_QPE=np.zeros((len(p0_array),len(T_list_QPE)))
        # max_T_QCELS=np.zeros((len(p0_array),len(T_list_QCELS)))
        # Navg = 3 #number of trying
        # err_thres_hold=0.01
        # err_thres_hold_QPE=0.01
        # #-----------------------------    
        # for a1 in range(len(p0_array)):
        #     p0=p0_array[a1]
        #     p1=p1_array[a1]
        #     n_success_QCELS= np.zeros(len(T_list_QCELS))
        #     n_success_QPE= np.zeros(len(T_list_QPE))
        #     for n_test in range(Navg):
        #         print("For p0=",p0," p1=", p1, "For N_test=",n_test+1)
        #         spectrum, population = generate_spectrum_population(eigenenergies, 
        #                 population_raw, [p0,p1])
        #         #------------------QCELS-----------------
        #         # heuristic estimate of relative gap
        #         rel_gap_idx = np.where(population>p0/2)[0][1] 
        #         rel_gap = spectrum[rel_gap_idx]-spectrum[0]
        #         d=int(20/rel_gap)
        #         print("d=", d, "rel_gap = ", rel_gap)
        #         Nsample_rough=int(500/p0**2*np.log(d))
        #         Nsample=int(30/p0**2*np.log(d))
        #         for ix in range(len(T_list_QCELS)):
        #             T = T_list_QCELS[ix]
        #             NT = 5
        #             err_tol_rough=rel_gap/4
        #             ground_energy_estimate_QCELS, cost_list_avg_QCELS[a1,ix], max_T_QCELS[a1,ix] = \
        #                     qcels_smalloverlap(spectrum, population, T, NT, d, rel_gap, \
        #                                     err_tol_rough, Nsample_rough, Nsample)
        #             err_this_run_QCELS = np.abs(ground_energy_estimate_QCELS - spectrum[0])
        #             err_QCELS[a1,ix] = err_QCELS[a1,ix]+np.abs(err_this_run_QCELS)
        #             if np.abs(err_this_run_QCELS)<err_thres_hold:
        #                 n_success_QCELS[ix]+=1
            
        #        # ----------------- QPE -----------------------
        #         N_try_QPE=int(15*np.ceil(1.0/p0))
        #         for ix in range(len(T_list_QPE)):
        #             T = int(T_list_QPE[ix])
        #             discrete_energies = 2*np.pi*np.arange(2*T)/(2*T) - np.pi
        #             dist = generate_QPE_distribution(spectrum,population,2*T)
        #             samp = generate_cdf.draw_with_prob(dist,N_try_QPE)
        #             j_min = samp.min()
        #             ground_energy_estimate_QPE = discrete_energies[j_min]
        #             err_this_run_QPE = np.abs(ground_energy_estimate_QPE-spectrum[0])
        #             err_QPE[a1,ix] = err_QPE[a1,ix]+np.abs(err_this_run_QPE)
        #             if np.abs(err_this_run_QPE)<err_thres_hold_QPE:
        #                 n_success_QPE[ix]+=1
        #             cost_list_avg_QPE[a1,ix] = T*N_try_QPE
        #     rate_success_QCELS[a1,:] = n_success_QCELS[:]/Navg
        #     rate_success_QPE[a1,:] = n_success_QPE[:]/Navg
        #     err_QCELS[a1,:] = err_QCELS[a1,:]/Navg
        #     err_QPE[a1,:] = err_QPE[a1,:]/Navg
        #     cost_list_avg_QCELS[a1,:]=cost_list_avg_QCELS[a1,:]/Navg


        # print('QCELS')
        # print(rate_success_QCELS)
        # print('QPE')
        # print(rate_success_QPE)    
        # plt.figure(figsize=(12,10))
        # plt.plot(T_list_QCELS,err_QCELS[0,:],linestyle="-.",marker="o",label="error of QCELS p_0=0.4,p_r=0.8")
        # plt.plot(T_list_QPE,err_QPE[0,:],linestyle="-.",marker="*",label="error of QPE p_0=0.4, p_r=0.8")
        # plt.xlabel("$T_{max}$",fontsize=35)
        # plt.ylabel("error($\epsilon$)",fontsize=35)
        # plt.xscale("log")
        # plt.yscale("log")
        # plt.legend()
        # plt.figure(figsize=(12,10))
        # plt.plot(cost_list_avg_QCELS[0,:],err_QCELS[0,:],linestyle="-.",marker="o",label="error of QCELS p_0=0.4, p_r=0.8")
        # plt.plot(cost_list_avg_QPE[0,:],err_QPE[0,:],linestyle="-.",marker="*",label="error of QPE p_0=0.4, p_r=0.8")
        # plt.xlabel("$T_{total}$",fontsize=35)
        # plt.ylabel("error($\epsilon$)",fontsize=35) 
        # plt.xscale("log")
        # plt.yscale("log")
        # plt.legend(fontsize=25)
        # plt.show()
