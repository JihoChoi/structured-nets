import numpy as np
import torch
import torch.nn as nn
from torch.nn.parameter import Parameter

from . import toeplitz as toep
from . import krylov as kry
from . import circulant as circ
from . import fastfood as ff

class StructuredLinear(nn.Module):
    class_type = None
    abbrev = None

    def name(self):
        return self.__class__.abbrev

    def __init__(self, layer_size=None, bias=True, **kwargs):
        super().__init__()
        self.layer_size = layer_size
        self.bias = bias
        self.__dict__.update(kwargs)
        self.reset_parameters()

    def reset_parameters(self):
        assert self.layer_size is not None
        self.b = None
        if self.bias:
            self.b = Parameter(torch.zeros(self.layer_size))

    def apply_bias(self, out):
        if self.b is not None:
            return self.b + out
        else:
            return out

    def loss(self):
        return 0

class Unconstrained(StructuredLinear):
    class_type = 'unconstrained'
    abbrev = 'u'

    def reset_parameters(self):
        super().reset_parameters()
        self.W = Parameter(torch.Tensor(self.layer_size, self.layer_size))
        self.init_stddev = np.sqrt(1./self.layer_size)
        torch.nn.init.normal_(self.W, std=self.init_stddev)

    def forward(self, x):
        out = torch.matmul(x, self.W)
        return self.apply_bias(out)


class Circulant(StructuredLinear):
    class_type = 'circulant'
    abbrev = 'c'

    def reset_parameters(self):
        super().reset_parameters()
        self.c = Parameter(torch.Tensor(self.layer_size))
        self.init_stddev = np.sqrt(1./self.layer_size)
        torch.nn.init.normal_(self.c, std=self.init_stddev)

    def forward(self, x):
        return self.apply_bias(circ.circulant_multiply(self.c, x, self.layer_size))


class FastFood(StructuredLinear):
    class_type = 'fastfood'
    abbrev = 'f'

    def reset_parameters(self):
        super().reset_parameters()
        # TODO: should this be initialized as non adaptive Fastfood? http://proceedings.mlr.press/v28/le13.pdf
        # S,G,B: diagonal, learnable parameters
        # P: permutation, fixed
        self.S = Parameter(torch.Tensor(self.layer_size))
        self.G = Parameter(torch.Tensor(self.layer_size))
        self.B = Parameter(torch.Tensor(self.layer_size))
        self.P = torch.tensor(np.random.permutation(self.layer_size))
        self.init_stddev = np.sqrt(1./self.layer_size)
        torch.nn.init.normal_(self.S, std=self.init_stddev)
        torch.nn.init.normal_(self.G, std=self.init_stddev)
        torch.nn.init.normal_(self.B, std=self.init_stddev)

    def forward(self, x):
        return self.apply_bias(ff.fastfood_multiply(self.S, self.G, self.B, self.P, x))

class LowRank(StructuredLinear):
    class_type = 'low_rank'
    abbrev = 'lr'

    def name(self):
        return self.__class__.abbrev + str(self.r)

    def __init__(self, layer_size, r=1, **kwargs):
        super().__init__(layer_size, r=r, **kwargs)

    def reset_parameters(self):
        super().reset_parameters()
        self.G = Parameter(torch.Tensor(self.r, self.layer_size))
        self.H = Parameter(torch.Tensor(self.r, self.layer_size))
        self.init_stddev = np.power(1. / (self.r * self.layer_size), 1/2)
        torch.nn.init.normal_(self.G, std=self.init_stddev)
        torch.nn.init.normal_(self.H, std=self.init_stddev)

    def forward(self, x):
        xH = torch.matmul(x, self.H.t())
        out = torch.matmul(xH, self.G)
        return self.apply_bias(out)


class ToeplitzLike(LowRank):
    class_type = 'toeplitz'
    abbrev = 't'

    def reset_parameters(self):
        super().reset_parameters()
        self.corner = False

    def forward(self, x):
        out = toep.toeplitz_mult(self.G, self.H, x, self.corner)
        return self.apply_bias(out)

class ToeplitzLikeC(ToeplitzLike):
    class_type = 'toeplitz_corner'
    abbrev = 'tc'

    def reset_parameters(self):
        super().reset_parameters()
        self.corner = True

class HankelLike(LowRank):
    class_type = 'hankel'
    abbrev = 'h'

    def forward(self, x):
        out = toep.toeplitz_mult(self.G, self.H, x, True)
        n = out.size(-1)
        reverse_index = torch.arange(n - 1, -1, -1, dtype=torch.long, device=out.device)
        return self.apply_bias(out[..., reverse_index])

class VandermondeLike(LowRank):
    class_type = 'vandermonde'
    abbrev = 'v'

    def reset_parameters(self):
        super().reset_parameters()
        self.diag = Parameter(torch.ones(self.layer_size))

    def forward(self, x):
        K_H = kry.Krylov(lambda v: self.diag * v, self.H)
        out = toep.toeplitz_krylov_multiply(self.G, torch.transpose(x @ K_H, 0,1))
        return self.apply_bias(out)


class LearnedOperator(LowRank):
    """
    Abstract class for learned displacement operators
    Contains parameters such as tie_operators
    """
    class_type = None # abstract
    abbrev = None

    def __init__(self, tie_operators=False, corner=False, **kwargs):
        super().__init__(tie_operators=tie_operators, corner=corner, **kwargs)

class LDRSubdiagonal(LearnedOperator):
    class_type = 'subdiagonal'
    abbrev = 'sd'

    def reset_parameters(self):
        super().reset_parameters()
        self.subd_A = Parameter(torch.ones(self.layer_size-1))
        if self.tie_operators:
            self.subd_B = self.subd_A
        else:
            self.subd_B = Parameter(torch.ones(self.layer_size-1))

    def forward(self, x):
        out = kry.subdiag_mult(self.subd_A, self.subd_B, self.G, self.H, x)
        return self.apply_bias(out)

class LDRSubdiagonalC(LDRSubdiagonal):
    class_type = 'subdiagonal_corner'
    abbrev = 'sdc'

    def reset_parameters(self):
        super().reset_parameters()
        self.corner_A = Parameter(torch.tensor(0.0))
        self.corner_B = Parameter(torch.tensor(0.0))

    def forward(self, x):
        out = kry.subdiag_mult_cuda(self.subd_A, self.subd_B, self.G, self.H, x, corner_A=self.corner_A, corner_B=self.corner_B)
        return self.apply_bias(out)

class LDRTridiagonal(LearnedOperator):
    class_type = 'tridiagonal'
    abbrev = 'td'

    def reset_parameters(self):
        super().reset_parameters()
        self.subd_A = Parameter(torch.ones(self.layer_size-1))
        self.diag_A = Parameter(torch.zeros(self.layer_size))
        self.supd_A = Parameter(torch.zeros(self.layer_size-1))
        if self.tie_operators:
            self.subd_B = self.subd_A
            self.diag_B = self.diag_A
            self.supd_B = self.supd_A
        else:
            self.subd_B = Parameter(torch.ones(self.layer_size-1))
            self.diag_B = Parameter(torch.zeros(self.layer_size))
            self.supd_B = Parameter(torch.zeros(self.layer_size-1))
        self.corners_A = (0.0,0.0)
        self.corners_B = (0.0,0.0)

    def forward(self, x):
        out = kry.tridiag_mult_slow(self.subd_A, self.diag_A, self.supd_A, self.subd_B, self.diag_B, self.supd_B, self.G, self.H, x, corners_A=self.corners_A, corners_B=self.corners_B)
        return self.apply_bias(out)

class LDRTridiagonalC(LDRTridiagonal):
    class_type = 'tridiagonal_corner'
    abbrev = 'tdc'

    def reset_parameters(self):
        super().reset_parameters()
        self.corners_A = (Parameter(torch.tensor(0.0)), Parameter(torch.tensor(0.0)))
        self.corners_B = (Parameter(torch.tensor(0.0)), Parameter(torch.tensor(0.0)))


# create a map from class names to the Python class
# TODO: should go in some utils file
def descendants(cls):
    """
    Get all subclasses (recursively) of class cls, not including itself
    Assumes no multiple inheritance
    """
    desc = []
    for subcls in cls.__subclasses__():
        desc.append(subcls)
        desc.extend(descendants(subcls))
    return desc

class_map = {}
for cls in descendants(StructuredLinear):
    if cls.class_type is None: continue
    class_map[cls.class_type] = cls
    class_map[cls.abbrev] = cls

    # else:
    #     assert 0, f"{self.__class__.__name__} does not support {self.class_type}"