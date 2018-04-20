import numpy as np
import torch
from torch.autograd import Variable
import scipy.fftpack as fft
import itertools
from scipy import signal

from krylov_multiply import Fft, Ifft, Rfft_fast, Irfft_fast, ComplexMult, complex_mult_slow


class KT_Toeplitz():
    """Multiply Krylov(A, v)^T @ u when A is zero except on the subdiagonal.
    """

    def __init__(self, n, f=0, batch_size=1, rank=1):
        m = int(np.log2(n))
        assert n == 1 << m, 'n must be a power of 2'
        self.n = n
        self.m = m
        self.batch_size = batch_size
        self.rank = rank

        self.eta = None
        if f != 0:
            mod = np.power(np.abs(f), np.arange(n)/n)
            if f > 0:
                arg = np.ones(n)
            else:
                arg = np.fft.fft(np.eye(1,2*n,2*n-1))[0,:n]
            # self.eta = mod * arg
            self.eta = Variable(torch.Tensor((mod * arg).astype('complex64').view('float32')), requires_grad=False).cuda()
            self.ieta = Variable(torch.Tensor((1/(mod * arg)).astype('complex64').view('float32')), requires_grad=False).cuda()


    def __call__(self, v, u):
        """
        Multiply Krylov(Z_f, v)^T @ u
        v: (rank, n)
        u: (batch, n)
        out: (batch, rank, n)
        """
        n, m, batch_size, rank = self.n, self.m, self.batch_size, self.rank

        if self.eta is not None: # cycle version
            u_ = Ifft.apply((self.ieta.view(n,2) * u.view(batch_size,n,1)).view(batch_size,2*n))
            v_ = Fft.apply((self.eta.view(n,2) * v.view(rank,n,1)).view(rank,2*n))
            uv_ = complex_mult_slow(u_.view(batch_size, 1, 2*n), v_.view(1, rank, 2*n))
            ans = complex_mult_slow(self.eta, Fft.apply(uv_))
            return ans[..., ::2]
        else:
            rev_idx_n = torch.arange(n-1, -1, -1, out=torch.cuda.LongTensor())
            # output of rfft has size (2n/2)+1 = n+1 complex numbers, so 2n+2 real comps
            rev_idx_2n = torch.arange(2*n+1, -1, -1, out=torch.cuda.LongTensor())

            u_ = Rfft_fast.apply(torch.cat((u[...,rev_idx_n], torch.zeros_like(u)), dim=-1))
            v_ = Rfft_fast.apply(torch.cat((v, torch.zeros_like(v)), dim=-1))
            uv_ = complex_mult_slow(u_.view(batch_size, 1, -1), v_.view(1, rank, -1))
            ans = Irfft_fast.apply(uv_)[..., rev_idx_n]
            return ans
    # TODO can this be done with rfft


class K_Toeplitz():
    """Multiply Krylov(A, v) @ w when A is zero except on the subdiagonal.
    """

    def __init__(self, n, f, batch_size=1, rank=1):
        m = int(np.log2(n))
        assert n == 1 << m, 'n must be a power of 2'
        self.n = n
        self.m = m
        self.batch_size = batch_size
        self.rank = rank

        self.eta = None
        if f != 0:
            mod = np.power(np.abs(f), np.arange(n)/n)
            if f > 0:
                arg = np.ones(n)
            else:
                arg = np.fft.fft(np.eye(1,2*n,2*n-1))[0,:n]
            # self.eta = mod * arg
            self.eta = Variable(torch.Tensor((mod * arg).astype('complex64').view('float32')), requires_grad=False).cuda()
            self.ieta = Variable(torch.Tensor((1/(mod * arg)).astype('complex64').view('float32')), requires_grad=False).cuda()

    def __call__(self, v, w):
        """
        v: (rank, n)
        w: (batch_size, rank, n)
        out: (batch_size, n)
        """
        n, m, batch_size, rank = self.n, self.m, self.batch_size, self.rank
        if self.eta is not None:
            weta = self.eta.view(n,2) * w.contiguous().view(batch_size, rank, n, 1)
            veta = self.eta.view(n,2) * v.contiguous().view(rank, n, 1)
            w_ = Fft.apply(weta.view(batch_size, rank, -1))
            v_ = Fft.apply(veta.view(rank, -1))
            wv_ = complex_mult_slow(w_, v_)
            ans = complex_mult_slow(self.ieta, Ifft.apply(torch.sum(wv_, dim=1)))
            return ans[..., ::2]
        else:
            w_ = Rfft_fast.apply(torch.cat((w, torch.zeros_like(w)), dim=-1))
            v_ = Rfft_fast.apply(torch.cat((v, torch.zeros_like(v)), dim=-1))
            wv_ = complex_mult_slow(w_, v_.view((1, rank, -1)))
            ans = Irfft_fast.apply(torch.sum(wv_, dim=1))[..., :n]
            return ans


def toeplitz_mult(G, H, x, cycle=True):
    rank, n = G.shape
    batch_size = x.shape[0]
    f = (1,-1) if cycle else (0,0)
    transpose_out = KT_Toeplitz(n, f[1], batch_size, rank)(H, x)
    krylov_out = K_Toeplitz(n, f[0], batch_size, rank)(G, transpose_out)
    return krylov_out


##### Slow mult

def krylov_construct(f, v, m):
    n = v.shape[0]
    K = np.zeros(shape=(m,n))
    K[0,:] = v
    for i in range(1,m):
        K[i,1:] = K[i-1,:-1]
        K[i,0] = f*K[i-1,-1]
    return K.T

def toeplitz_mult_slow(G, H, x, cycle=True):
    assert G.shape == H.shape
    rank, n = G.shape
    f = (1,-1) if cycle else (0,0)
    krylovs = [(krylov_construct(f[0], G[i], n), krylov_construct(f[1], H[i], n).T) for i in range(rank)]
    prods = [K[0] @ K[1] @ x.T for K in krylovs]
    return np.sum(np.array(prods), axis=0).T

if __name__ == '__main__':
    v = Variable(torch.Tensor([[0,1,0,-1],[0,1,2,3]])).cuda()
    u = Variable(torch.Tensor([[1,1,1,1],[0,1,2,3]])).cuda()

    w = KT_Toeplitz(4, -1, 2, 2)(v, u)
    # output:
    # [[[ 0 2  2 0]
    #   [ 6 0 -4 -6]]

    #  [[ -2 2 4  2]
    #   [ 14 8 0 -8]]]

    toeplitz_mult(v, v, u)
    # toeplitz_mult_slow(v, v, u)
    # output:
    # array([[-16., -20.,  -4.,  16.],
    #        [ 16.,  -8.,  12.,  64.]])

    toeplitz_mult(v, v, u, cycle=False)
    # toeplitz_mult_slow(v, v, u, cycle=False)
    # output:
    # array([[ 0.,  6., 16., 26.],
    #        [ 0., 12., 38., 66.]])

n=4
a = Variable(torch.Tensor([[1, 1, 1, 1], [0, 1, 2, 3]])).cuda()
ans = KT_Toeplitz(n,1,2,2)(a,a)
