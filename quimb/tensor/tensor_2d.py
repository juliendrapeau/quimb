"""Classes and algorithms related to 2D tensor networks.
"""
import random
import functools
from operator import add
from numbers import Integral
from itertools import product, cycle, starmap, combinations, count, chain
from collections import defaultdict

from autoray import do, infer_backend, get_dtype_name
import opt_einsum as oe

from ..gen.operators import swap
from ..gen.rand import randn, seed_rand
from ..utils import print_multi_line, check_opt, pairwise
from . import array_ops as ops
from .tensor_core import (
    Tensor,
    bonds,
    rand_uuid,
    oset,
    tags_to_oset,
    TensorNetwork,
    tensor_contract,
)
from .tensor_1d import maybe_factor_gate_into_tensor, rand_padder


def manhattan_distance(coo_a, coo_b):
    return sum(abs(coo_a[i] - coo_b[i]) for i in range(2))


def nearest_neighbors(coo):
    i, j = coo
    return ((i - 1, j), (i, j - 1), (i, j + 1), (i + 1, j))


class TensorNetwork2D(TensorNetwork):
    r"""Mixin class for tensor networks with a square lattice two-dimensional
    structure, indexed by ``[{row},{column}]`` so that::

                     'COL{j}'
                        v

        i=Lx-1 ●──●──●──●──●──●──   ──●
               |  |  |  |  |  |       |
                     ...
               |  |  |  |  |  | 'I{i},{j}' = 'I3,5' e.g.
        i=3    ●──●──●──●──●──●──
               |  |  |  |  |  |       |
        i=2    ●──●──●──●──●──●──   ──●    <== 'ROW{i}'
               |  |  |  |  |  |  ...  |
        i=1    ●──●──●──●──●──●──   ──●
               |  |  |  |  |  |       |
        i=0    ●──●──●──●──●──●──   ──●

             j=0, 1, 2, 3, 4, 5    j=Ly-1

    This implies the following conventions:

        * the 'up' bond is coordinates ``(i, j), (i + 1, j)``
        * the 'down' bond is coordinates ``(i, j), (i - 1, j)``
        * the 'right' bond is coordinates ``(i, j), (i, j + 1)``
        * the 'left' bond is coordinates ``(i, j), (i, j - 1)``

    """

    _EXTRA_PROPS = (
        '_site_tag_id',
        '_row_tag_id',
        '_col_tag_id',
        '_Lx',
        '_Ly',
    )

    def _compatible_2d(self, other):
        """Check whether ``self`` and ``other`` are compatible 2D tensor
        networks such that they can remain a 2D tensor network when combined.
        """
        return (
            isinstance(other, TensorNetwork2D) and
            all(getattr(self, e) == getattr(other, e)
                for e in TensorNetwork2D._EXTRA_PROPS)
        )

    def __and__(self, other):
        new = super().__and__(other)
        if self._compatible_2d(other):
            new.view_as_(TensorNetwork2D, like=self)
        return new

    def __or__(self, other):
        new = super().__or__(other)
        if self._compatible_2d(other):
            new.view_as_(TensorNetwork2D, like=self)
        return new

    @property
    def Lx(self):
        """The number of rows.
        """
        return self._Lx

    @property
    def Ly(self):
        """The number of columns.
        """
        return self._Ly

    @property
    def site_tag_id(self):
        """The string specifier for tagging each site of this 2D TN.
        """
        return self._site_tag_id

    def site_tag(self, i, j):
        """The name of the tag specifiying the tensor at site ``(i, j)``.
        """
        if not isinstance(i, str):
            i = i % self.Lx
        if not isinstance(j, str):
            j = j % self.Ly
        return self.site_tag_id.format(i, j)

    @property
    def row_tag_id(self):
        """The string specifier for tagging each row of this 2D TN.
        """
        return self._row_tag_id

    def row_tag(self, i):
        if not isinstance(i, str):
            i = i % self.Lx
        return self.row_tag_id.format(i)

    @property
    def row_tags(self):
        """A tuple of all of the ``Lx`` different row tags.
        """
        return tuple(map(self.row_tag, range(self.Lx)))

    @property
    def col_tag_id(self):
        """The string specifier for tagging each column of this 2D TN.
        """
        return self._col_tag_id

    def col_tag(self, j):
        if not isinstance(j, str):
            j = j % self.Ly
        return self.col_tag_id.format(j)

    @property
    def col_tags(self):
        """A tuple of all of the ``Ly`` different column tags.
        """
        return tuple(map(self.col_tag, range(self.Ly)))

    @property
    def site_tags(self):
        """All of the ``Lx * Ly`` site tags.
        """
        return tuple(starmap(self.site_tag, self.gen_site_coos()))

    def maybe_convert_coo(self, x):
        """Check if ``x`` is a tuple of two ints and convert to the
        corresponding site tag if so.
        """
        if not isinstance(x, str):
            try:
                i, j = map(int, x)
                return self.site_tag(i, j)
            except (ValueError, TypeError):
                pass
        return x

    def _get_tids_from_tags(self, tags, which='all'):
        """This is the function that lets coordinates such as ``(i, j)`` be
        used for many 'tag' based functions.
        """
        tags = self.maybe_convert_coo(tags)
        return super()._get_tids_from_tags(tags, which=which)

    def gen_site_coos(self):
        """Generate coordinates for all the sites in this 2D TN.
        """
        return product(range(self.Lx), range(self.Ly))

    def gen_bond_coos(self):
        """Generate pairs of coordinates for all the bonds in this 2D TN.
        """
        for i, j in self.gen_site_coos():
            coo_right = (i, j + 1)
            if self.valid_coo(coo_right):
                yield (i, j), coo_right
            coo_above = (i + 1, j)
            if self.valid_coo(coo_above):
                yield (i, j), coo_above

    def gen_horizontal_bond_coos(self):
        """Generate all coordinate pairs like ``(i, j), (i, j + 1)``.
        """
        for i in range(self.Lx):
            for j in range(self.Ly - 1):
                yield (i, j), (i, j + 1)

    def gen_horizontal_even_bond_coos(self):
        """Generate all coordinate pairs like ``(i, j), (i, j + 1)`` where
        ``j`` is even, which thus don't overlap at all.
        """
        for i in range(self.Lx):
            for j in range(0, self.Ly - 1, 2):
                yield (i, j), (i, j + 1)

    def gen_horizontal_odd_bond_coos(self):
        """Generate all coordinate pairs like ``(i, j), (i, j + 1)`` where
        ``j`` is odd, which thus don't overlap at all.
        """
        for i in range(self.Lx):
            for j in range(1, self.Ly - 1, 2):
                yield (i, j), (i, j + 1)

    def gen_vertical_bond_coos(self):
        """Generate all coordinate pairs like ``(i, j), (i + 1, j)``.
        """
        for j in range(self.Ly):
            for i in range(self.Lx - 1):
                yield (i, j), (i + 1, j)

    def gen_vertical_even_bond_coos(self):
        """Generate all coordinate pairs like ``(i, j), (i + 1, j)`` where
        ``i`` is even, which thus don't overlap at all.
        """
        for j in range(self.Ly):
            for i in range(0, self.Lx - 1, 2):
                yield (i, j), (i + 1, j)

    def gen_vertical_odd_bond_coos(self):
        """Generate all coordinate pairs like ``(i, j), (i + 1, j)`` where
        ``i`` is odd, which thus don't overlap at all.
        """
        for j in range(self.Ly):
            for i in range(1, self.Lx - 1, 2):
                yield (i, j), (i + 1, j)

    def valid_coo(self, ij):
        """Test whether ``ij`` is in grid for this 2D TN.
        """
        i, j = ij
        return (0 <= i < self.Lx) and (0 <= j < self.Ly)

    def __repr__(self):
        """Insert number of rows and columns into standard print.
        """
        s = super().__repr__()
        extra = f', Lx={self.Lx}, Ly={self.Ly}, max_bond={self.max_bond()}'
        s = f'{s[:-2]}{extra}{s[-2:]}'
        return s

    def __str__(self):
        """Insert number of rows and columns into standard print.
        """
        s = super().__str__()
        extra = f', Lx={self.Lx}, Ly={self.Ly}, max_bond={self.max_bond()}'
        s = f'{s[:-1]}{extra}{s[-1:]}'
        return s

    def flatten(self, fuse_multibonds=True, inplace=False):
        """Contract all tensors corresponding to each site into one.
        """
        tn = self if inplace else self.copy()

        for i, j in self.gen_site_coos():
            tn ^= (i, j)

        if fuse_multibonds:
            tn.fuse_multibonds_()

        return tn.view_as_(TensorNetwork2DFlat, like=self)

    flatten_ = functools.partialmethod(flatten, inplace=True)

    def canonize_row(self, i, sweep, yrange=None, **canonize_opts):
        r"""Canonize all or part of a row.

        If ``sweep == 'right'`` then::

             |  |  |  |  |  |  |         |  |  |  |  |  |  |
            ─●──●──●──●──●──●──●─       ─●──●──●──●──●──●──●─
             |  |  |  |  |  |  |         |  |  |  |  |  |  |
            ─●──●──●──●──●──●──●─  ==>  ─●──>──>──>──>──o──●─ row=i
             |  |  |  |  |  |  |         |  |  |  |  |  |  |
            ─●──●──●──●──●──●──●─       ─●──●──●──●──●──●──●─
             |  |  |  |  |  |  |         |  |  |  |  |  |  |
                .           .               .           .
                jstart      jstop           jstart      jstop

        If ``sweep == 'left'`` then::

             |  |  |  |  |  |  |         |  |  |  |  |  |  |
            ─●──●──●──●──●──●──●─       ─●──●──●──●──●──●──●─
             |  |  |  |  |  |  |         |  |  |  |  |  |  |
            ─●──●──●──●──●──●──●─  ==>  ─●──o──<──<──<──<──●─ row=i
             |  |  |  |  |  |  |         |  |  |  |  |  |  |
            ─●──●──●──●──●──●──●─       ─●──●──●──●──●──●──●─
             |  |  |  |  |  |  |         |  |  |  |  |  |  |
                .           .               .           .
                jstop       jstart          jstop       jstart

        Does not yield an orthogonal form in the same way as in 1D.

        Parameters
        ----------
        i : int
            Which row to canonize.
        sweep : {'right', 'left'}
            Which direction to sweep in.
        jstart : int or None
            Starting column, defaults to whole row.
        jstop : int or None
            Stopping column, defaults to whole row.
        canonize_opts
            Supplied to ``canonize_between``.
        """
        check_opt('sweep', sweep, ('right', 'left'))

        if yrange is None:
            yrange = (0, self.Ly - 1)

        if sweep == 'right':
            for j in range(min(yrange), max(yrange), +1):
                self.canonize_between((i, j), (i, j + 1), **canonize_opts)

        else:
            for j in range(max(yrange), min(yrange), -1):
                self.canonize_between((i, j), (i, j - 1), **canonize_opts)

    def canonize_column(self, j, sweep, xrange=None, **canonize_opts):
        r"""Canonize all or part of a column.

        If ``sweep='up'`` then::

             |  |  |         |  |  |
            ─●──●──●─       ─●──●──●─
             |  |  |         |  |  |
            ─●──●──●─       ─●──o──●─ istop
             |  |  |   ==>   |  |  |
            ─●──●──●─       ─●──^──●─
             |  |  |         |  |  |
            ─●──●──●─       ─●──^──●─ istart
             |  |  |         |  |  |
            ─●──●──●─       ─●──●──●─
             |  |  |         |  |  |
                .               .
                j               j

        If ``sweep='down'`` then::

             |  |  |         |  |  |
            ─●──●──●─       ─●──●──●─
             |  |  |         |  |  |
            ─●──●──●─       ─●──v──●─ istart
             |  |  |   ==>   |  |  |
            ─●──●──●─       ─●──v──●─
             |  |  |         |  |  |
            ─●──●──●─       ─●──o──●─ istop
             |  |  |         |  |  |
            ─●──●──●─       ─●──●──●─
             |  |  |         |  |  |
                .               .
                j               j

        Does not yield an orthogonal form in the same way as in 1D.

        Parameters
        ----------
        j : int
            Which column to canonize.
        sweep : {'up', 'down'}
            Which direction to sweep in.
        xrange : None or (int, int), optional
            The range of columns to canonize.
        canonize_opts
            Supplied to ``canonize_between``.
        """
        check_opt('sweep', sweep, ('up', 'down'))

        if xrange is None:
            xrange = (0, self.Lx - 1)

        if sweep == 'up':
            for i in range(min(xrange), max(xrange), +1):
                self.canonize_between((i, j), (i + 1, j), **canonize_opts)
        else:
            for i in range(max(xrange), min(xrange), -1):
                self.canonize_between((i, j), (i - 1, j), **canonize_opts)

    def canonize_row_around(self, i, around=(0, 1)):
        # sweep to the right
        self.canonize_row(i, 'right', yrange=(0, min(around)))
        # sweep to the left
        self.canonize_row(i, 'left', yrange=(max(around), self.Ly - 1))

    def compress_row(self, i, sweep, yrange=None, **compress_opts):
        r"""Compress all or part of a row.

        If ``sweep == 'right'`` then::

             |  |  |  |  |  |  |         |  |  |  |  |  |  |
            ━●━━●━━●━━●━━●━━●━━●━       ━●━━●━━●━━●━━●━━●━━●━
             |  |  |  |  |  |  |         |  |  |  |  |  |  |
            ━●━━●━━●━━●━━●━━●━━●━  ━━>  ━●━━>──>──>──>──o━━●━ row=i
             |  |  |  |  |  |  |         |  |  |  |  |  |  |
            ━●━━●━━●━━●━━●━━●━━●━       ━●━━●━━●━━●━━●━━●━━●━
             |  |  |  |  |  |  |         |  |  |  |  |  |  |
                .           .               .           .
                jstart      jstop           jstart      jstop

        If ``sweep == 'left'`` then::

             |  |  |  |  |  |  |         |  |  |  |  |  |  |
            ━●━━●━━●━━●━━●━━●━━●━       ━●━━●━━●━━●━━●━━●━━●━
             |  |  |  |  |  |  |         |  |  |  |  |  |  |
            ━●━━●━━●━━●━━●━━●━━●━  ━━>  ━●━━o──<──<──<──<━━●━ row=i
             |  |  |  |  |  |  |         |  |  |  |  |  |  |
            ━●━━●━━●━━●━━●━━●━━●━       ━●━━●━━●━━●━━●━━●━━●━
             |  |  |  |  |  |  |         |  |  |  |  |  |  |
                .           .               .           .
                jstop       jstart          jstop       jstart

        Does not yield an orthogonal form in the same way as in 1D.

        Parameters
        ----------
        i : int
            Which row to compress.
        sweep : {'right', 'left'}
            Which direction to sweep in.
        jstart : int or None
            Starting column, defaults to whole row.
        jstop : int or None
            Stopping column, defaults to whole row.
        compress_opts
            Supplied to
            :meth:`~quimb.tensor.tensor_core.TensorNetwork.compress_between`.
        """
        check_opt('sweep', sweep, ('right', 'left'))
        compress_opts.setdefault('absorb', 'right')

        if yrange is None:
            yrange = (0, self.Ly - 1)

        if sweep == 'right':
            for j in range(min(yrange), max(yrange), +1):
                self.compress_between((i, j), (i, j + 1), **compress_opts)
        else:
            for j in range(max(yrange), min(yrange), -1):
                self.compress_between((i, j), (i, j - 1), **compress_opts)

    def compress_column(self, j, sweep, xrange=None, **compress_opts):
        r"""Compress all or part of a column.

        If ``sweep='up'`` then::

             ┃  ┃  ┃         ┃  ┃  ┃
            ─●──●──●─       ─●──●──●─
             ┃  ┃  ┃         ┃  ┃  ┃
            ─●──●──●─       ─●──o──●─  .
             ┃  ┃  ┃   ==>   ┃  |  ┃   .
            ─●──●──●─       ─●──^──●─  . xrange
             ┃  ┃  ┃         ┃  |  ┃   .
            ─●──●──●─       ─●──^──●─  .
             ┃  ┃  ┃         ┃  ┃  ┃
            ─●──●──●─       ─●──●──●─
             ┃  ┃  ┃         ┃  ┃  ┃
                .               .
                j               j

        If ``sweep='down'`` then::

             ┃  ┃  ┃         ┃  ┃  ┃
            ─●──●──●─       ─●──●──●─
             ┃  ┃  ┃         ┃  ┃  ┃
            ─●──●──●─       ─●──v──●─ .
             ┃  ┃  ┃   ==>   ┃  |  ┃  .
            ─●──●──●─       ─●──v──●─ . xrange
             ┃  ┃  ┃         ┃  |  ┃  .
            ─●──●──●─       ─●──o──●─ .
             ┃  ┃  ┃         ┃  ┃  ┃
            ─●──●──●─       ─●──●──●─
             ┃  ┃  ┃         ┃  ┃  ┃
                .               .
                j               j

        Does not yield an orthogonal form in the same way as in 1D.

        Parameters
        ----------
        j : int
            Which column to compress.
        sweep : {'up', 'down'}
            Which direction to sweep in.
        xrange : None or (int, int), optional
            The range of rows to compress.
        compress_opts
            Supplied to
            :meth:`~quimb.tensor.tensor_core.TensorNetwork.compress_between`.
        """
        check_opt('sweep', sweep, ('up', 'down'))

        if xrange is None:
            xrange = (0, self.Lx - 1)

        if sweep == 'up':
            compress_opts.setdefault('absorb', 'right')
            for i in range(min(xrange), max(xrange), +1):
                self.compress_between((i, j), (i + 1, j), **compress_opts)
        else:
            compress_opts.setdefault('absorb', 'left')
            for i in range(max(xrange), min(xrange), -1):
                self.compress_between((i - 1, j), (i, j), **compress_opts)

    def __getitem__(self, key):
        """Key based tensor selection, checking for integer based shortcut.
        """
        return super().__getitem__(self.maybe_convert_coo(key))

    def show(self):
        """Print a unicode schematic of this PEPS and its bond dimensions.
        """
        show_2d(self)

    def _contract_boundary_from_bottom_single(
        self,
        xrange,
        yrange,
        canonize=True,
        compress_sweep='left',
        layer_tag=None,
        **compress_opts
    ):
        canonize_sweep = {
            'left': 'right',
            'right': 'left',
        }[compress_sweep]

        for i in range(min(xrange), max(xrange)):
            #
            #     │  │  │  │  │
            #     ●──●──●──●──●       │  │  │  │  │
            #     │  │  │  │  │  -->  ●══●══●══●══●
            #     ●──●──●──●──●
            #
            for j in range(min(yrange), max(yrange) + 1):
                tag1, tag2 = self.site_tag(i, j), self.site_tag(i + 1, j)

                if layer_tag is None:
                    # contract any tensors with coordinates (i + 1, j), (i, j)
                    self.contract_((tag1, tag2), which='any')
                else:
                    # contract a specific pair (i.e. only one 'inner' layer)
                    self.contract_between(tag1, (tag2, layer_tag))

            if canonize:
                #
                #     │  │  │  │  │
                #     ●══●══<══<══<
                #
                self.canonize_row(i, sweep=canonize_sweep, yrange=yrange)

            #
            #     │  │  │  │  │  -->  │  │  │  │  │  -->  │  │  │  │  │
            #     >──●══●══●══●  -->  >──>──●══●══●  -->  >──>──>──●══●
            #     .  .           -->     .  .        -->        .  .
            #
            self.compress_row(i, sweep=compress_sweep,
                              yrange=yrange, **compress_opts)

    def _contract_boundary_from_bottom_multi(
        self,
        xrange,
        yrange,
        layer_tags,
        canonize=True,
        compress_sweep='left',
        **compress_opts
    ):
        for i in range(min(xrange), max(xrange)):
            # make sure the exterior sites are a single tensor
            #
            #    │ ││ ││ ││ ││ │       │ ││ ││ ││ ││ │   (for two layer tags)
            #    ●─○●─○●─○●─○●─○       ●─○●─○●─○●─○●─○
            #    │ ││ ││ ││ ││ │  ==>   ╲│ ╲│ ╲│ ╲│ ╲│
            #    ●─○●─○●─○●─○●─○         ●══●══●══●══●
            #
            for j in range(min(yrange), max(yrange) + 1):
                self ^= (i, j)

            for tag in layer_tags:
                # contract interior sites from layer ``tag``
                #
                #    │ ││ ││ ││ ││ │  (first contraction if there are two tags)
                #    │ ○──○──○──○──○
                #    │╱ │╱ │╱ │╱ │╱
                #    ●══<══<══<══<
                #
                self._contract_boundary_from_bottom_single(
                    xrange=(i, i + 1), yrange=yrange, canonize=canonize,
                    compress_sweep=compress_sweep, layer_tag=tag,
                    **compress_opts)

                # so we can still uniqely identify 'inner' tensors, drop inner
                #     site tag merged into outer tensor for all but last tensor
                for j in range(min(yrange), max(yrange) + 1):
                    inner_tag = self.site_tag(i + 1, j)
                    if len(self.tag_map[inner_tag]) > 1:
                        self[i, j].drop_tags(inner_tag)

    def contract_boundary_from_bottom(
        self,
        xrange,
        yrange=None,
        canonize=True,
        compress_sweep='left',
        layer_tags=None,
        inplace=False,
        **compress_opts
    ):
        """Contract a 2D tensor network inwards from the bottom, canonizing and
        compressing (left to right) along the way.

        Parameters
        ----------
        xrange : (int, int)
            The range of rows to compress (inclusive).
        yrange : (int, int) or None, optional
            The range of columns to compress (inclusive), sweeping along with
            canonization and compression. Defaults to all columns.
        canonize : bool, optional
            Whether to sweep one way with canonization before compressing.
        compress_sweep : {'left', 'right'}, optional
            Which way to perform the compression sweep, which has an effect on
            which tensors end up being canonized.
        layer_tags : None or sequence[str], optional
            If ``None``, all tensors at each coordinate pair
            ``[(i, j), (i + 1, j)]`` will be first contracted. If specified,
            then the outer tensor at ``(i, j)`` will be contracted with the
            tensor specified by ``[(i + 1, j), layer_tag]``, for each
            ``layer_tag`` in ``layer_tags``.
        inplace : bool, optional
            Whether to perform the contraction inplace or not.
        compress_opts
            Supplied to
            :meth:`~quimb.tensor.tensor_2d.TensorNetwork2D.compress_row`.

        See Also
        --------
        contract_boundary_from_top, contract_boundary_from_left,
        contract_boundary_from_right
        """
        tn = self if inplace else self.copy()

        if yrange is None:
            yrange = (0, self.Ly - 1)

        if layer_tags is None:
            tn._contract_boundary_from_bottom_single(
                xrange, yrange, canonize=canonize,
                compress_sweep=compress_sweep, **compress_opts)
        else:
            tn._contract_boundary_from_bottom_multi(
                xrange, yrange, layer_tags, canonize=canonize,
                compress_sweep=compress_sweep, **compress_opts)

        return tn

    contract_boundary_from_bottom_ = functools.partialmethod(
        contract_boundary_from_bottom, inplace=True)

    def _contract_boundary_from_top_single(
        self,
        xrange,
        yrange,
        canonize=True,
        compress_sweep='right',
        layer_tag=None,
        **compress_opts
    ):
        canonize_sweep = {
            'left': 'right',
            'right': 'left',
        }[compress_sweep]

        for i in range(max(xrange), min(xrange), -1):
            #
            #     ●──●──●──●──●
            #     |  |  |  |  |  -->  ●══●══●══●══●
            #     ●──●──●──●──●       |  |  |  |  |
            #     |  |  |  |  |
            #
            for j in range(min(yrange), max(yrange) + 1):
                tag1, tag2 = self.site_tag(i, j), self.site_tag(i - 1, j)
                if layer_tag is None:
                    # contract any tensors with coordinates (i - 1, j), (i, j)
                    self.contract_((tag1, tag2), which='any')
                else:
                    # contract a specific pair
                    self.contract_between(tag1, (tag2, layer_tag))
            if canonize:
                #
                #     ●══●══<══<══<
                #     |  |  |  |  |
                #
                self.canonize_row(i, sweep=canonize_sweep, yrange=yrange)
            #
            #     >──●══●══●══●  -->  >──>──●══●══●  -->  >──>──>──●══●
            #     |  |  |  |  |  -->  |  |  |  |  |  -->  |  |  |  |  |
            #     .  .           -->     .  .        -->        .  .
            #
            self.compress_row(i, sweep=compress_sweep,
                              yrange=yrange, **compress_opts)

    def _contract_boundary_from_top_multi(
        self,
        xrange,
        yrange,
        layer_tags,
        canonize=True,
        compress_sweep='left',
        **compress_opts
    ):
        for i in range(max(xrange), min(xrange), -1):
            # make sure the exterior sites are a single tensor
            #
            #    ●─○●─○●─○●─○●─○         ●══●══●══●══●
            #    │ ││ ││ ││ ││ │  ==>   ╱│ ╱│ ╱│ ╱│ ╱│
            #    ●─○●─○●─○●─○●─○       ●─○●─○●─○●─○●─○
            #    │ ││ ││ ││ ││ │       │ ││ ││ ││ ││ │   (for two layer tags)
            #
            for j in range(min(yrange), max(yrange) + 1):
                self ^= (i, j)

            for tag in layer_tags:
                # contract interior sites from layer ``tag``
                #
                #    ●══<══<══<══<
                #    │╲ │╲ │╲ │╲ │╲
                #    │ ○──○──○──○──○
                #    │ ││ ││ ││ ││ │  (first contraction if there are two tags)
                #
                self._contract_boundary_from_top_single(
                    xrange=(i, i - 1), yrange=yrange, canonize=canonize,
                    compress_sweep=compress_sweep, layer_tag=tag,
                    **compress_opts)

                # so we can still uniqely identify 'inner' tensors, drop inner
                #     site tag merged into outer tensor for all but last tensor
                for j in range(min(yrange), max(yrange) + 1):
                    inner_tag = self.site_tag(i - 1, j)
                    if len(self.tag_map[inner_tag]) > 1:
                        self[i, j].drop_tags(inner_tag)

    def contract_boundary_from_top(
        self,
        xrange,
        yrange=None,
        canonize=True,
        compress_sweep='right',
        layer_tags=None,
        inplace=False,
        **compress_opts
    ):
        """Contract a 2D tensor network inwards from the top, canonizing and
        compressing (left to right) along the way.

        Parameters
        ----------
        xrange : (int, int)
            The range of rows to compress (inclusive).
        yrange : (int, int) or None, optional
            The range of columns to compress (inclusive), sweeping along with
            canonization and compression. Defaults to all columns.
        canonize : bool, optional
            Whether to sweep one way with canonization before compressing.
        compress_sweep : {'right', 'left'}, optional
            Which way to perform the compression sweep, which has an effect on
            which tensors end up being canonized.
        layer_tags : None or str, optional
            If ``None``, all tensors at each coordinate pair
            ``[(i, j), (i - 1, j)]`` will be first contracted. If specified,
            then the outer tensor at ``(i, j)`` will be contracted with the
            tensor specified by ``[(i - 1, j), layer_tag]``, for each
            ``layer_tag`` in ``layer_tags``.
        inplace : bool, optional
            Whether to perform the contraction inplace or not.
        compress_opts
            Supplied to
            :meth:`~quimb.tensor.tensor_2d.TensorNetwork2D.compress_row`.

        See Also
        --------
        contract_boundary_from_bottom, contract_boundary_from_left,
        contract_boundary_from_right
        """
        tn = self if inplace else self.copy()

        if yrange is None:
            yrange = (0, self.Ly - 1)

        if layer_tags is None:
            tn._contract_boundary_from_top_single(
                xrange, yrange, canonize=canonize,
                compress_sweep=compress_sweep, **compress_opts)
        else:
            tn._contract_boundary_from_top_multi(
                xrange, yrange, layer_tags, canonize=canonize,
                compress_sweep=compress_sweep, **compress_opts)

        return tn

    contract_boundary_from_top_ = functools.partialmethod(
        contract_boundary_from_top, inplace=True)

    def _contract_boundary_from_left_single(
        self,
        yrange,
        xrange,
        canonize=True,
        compress_sweep='up',
        layer_tag=None,
        **compress_opts
    ):
        canonize_sweep = {
            'up': 'down',
            'down': 'up',
        }[compress_sweep]

        for j in range(min(yrange), max(yrange)):
            #
            #     ●──●──       ●──
            #     │  │         ║
            #     ●──●──  ==>  ●──
            #     │  │         ║
            #     ●──●──       ●──
            #
            for i in range(min(xrange), max(xrange) + 1):
                tag1, tag2 = self.site_tag(i, j), self.site_tag(i, j + 1)
                if layer_tag is None:
                    # contract any tensors with coordinates (i, j), (i, j + 1)
                    self.contract_((tag1, tag2), which='any')
                else:
                    # contract a specific pair
                    self.contract_between(tag1, (tag2, layer_tag))
            if canonize:
                #
                #     ●──       v──
                #     ║         ║
                #     ●──  ==>  v──
                #     ║         ║
                #     ●──       ●──
                #
                self.canonize_column(j, sweep=canonize_sweep, xrange=xrange)
            #
            #     v──       ●──
            #     ║         │
            #     v──  ==>  ^──
            #     ║         │
            #     ●──       ^──
            #
            self.compress_column(j, sweep=compress_sweep,
                                 xrange=xrange, **compress_opts)

    def _contract_boundary_from_left_multi(
        self,
        yrange,
        xrange,
        layer_tags,
        canonize=True,
        compress_sweep='up',
        **compress_opts
    ):
        for j in range(min(yrange), max(yrange)):
            # make sure the exterior sites are a single tensor
            #
            #     ○──○──           ●──○──
            #     │╲ │╲            │╲ │╲       (for two layer tags)
            #     ●─○──○──         ╰─●──○──
            #      ╲│╲╲│╲     ==>    │╲╲│╲
            #       ●─○──○──         ╰─●──○──
            #        ╲│ ╲│             │ ╲│
            #         ●──●──           ╰──●──
            #
            for i in range(min(xrange), max(xrange) + 1):
                self ^= (i, j)

            for tag in layer_tags:
                # contract interior sites from layer ``tag``
                #
                #        ○──
                #      ╱╱ ╲        (first contraction if there are two tags)
                #     ●─── ○──
                #      ╲ ╱╱ ╲
                #       ^─── ○──
                #        ╲ ╱╱
                #         ^─────
                #
                self._contract_boundary_from_left_single(
                    yrange=(j, j + 1), xrange=xrange, canonize=canonize,
                    compress_sweep=compress_sweep, layer_tag=tag,
                    **compress_opts)

                # so we can still uniqely identify 'inner' tensors, drop inner
                #     site tag merged into outer tensor for all but last tensor
                for i in range(min(xrange), max(xrange) + 1):
                    inner_tag = self.site_tag(i, j + 1)
                    if len(self.tag_map[inner_tag]) > 1:
                        self[i, j].drop_tags(inner_tag)

    def contract_boundary_from_left(
        self,
        yrange,
        xrange=None,
        canonize=True,
        compress_sweep='up',
        layer_tags=None,
        inplace=False,
        **compress_opts
    ):
        """Contract a 2D tensor network inwards from the left, canonizing and
        compressing (top to bottom) along the way.

        Parameters
        ----------
        yrange : (int, int)
            The range of columns to compress (inclusive).
        xrange : (int, int) or None, optional
            The range of rows to compress (inclusive), sweeping along with
            canonization and compression. Defaults to all rows.
        canonize : bool, optional
            Whether to sweep one way with canonization before compressing.
        compress_sweep : {'up', 'down'}, optional
            Which way to perform the compression sweep, which has an effect on
            which tensors end up being canonized.
        layer_tags : None or str, optional
            If ``None``, all tensors at each coordinate pair
            ``[(i, j), (i, j + 1)]`` will be first contracted. If specified,
            then the outer tensor at ``(i, j)`` will be contracted with the
            tensor specified by ``[(i + 1, j), layer_tag]``, for each
            ``layer_tag`` in ``layer_tags``.
        inplace : bool, optional
            Whether to perform the contraction inplace or not.
        compress_opts
            Supplied to
            :meth:`~quimb.tensor.tensor_2d.TensorNetwork2D.compress_column`.

        See Also
        --------
        contract_boundary_from_bottom, contract_boundary_from_top,
        contract_boundary_from_right
        """
        tn = self if inplace else self.copy()

        if xrange is None:
            xrange = (0, self.Lx - 1)

        if layer_tags is None:
            tn._contract_boundary_from_left_single(
                yrange, xrange, canonize=canonize,
                compress_sweep=compress_sweep, **compress_opts)
        else:
            tn._contract_boundary_from_left_multi(
                yrange, xrange, layer_tags, canonize=canonize,
                compress_sweep=compress_sweep, **compress_opts)

        return tn

    contract_boundary_from_left_ = functools.partialmethod(
        contract_boundary_from_left, inplace=True)

    def _contract_boundary_from_right_single(
        self,
        yrange,
        xrange,
        canonize=True,
        compress_sweep='down',
        layer_tag=None,
        **compress_opts
    ):
        canonize_sweep = {
            'up': 'down',
            'down': 'up',
        }[compress_sweep]

        for j in range(max(yrange), min(yrange), -1):
            #
            #     ──●──●       ──●
            #       │  │         ║
            #     ──●──●  ==>  ──●
            #       │  │         ║
            #     ──●──●       ──●
            #
            for i in range(min(xrange), max(xrange) + 1):
                tag1, tag2 = self.site_tag(i, j), self.site_tag(i, j - 1)
                if layer_tag is None:
                    # contract any tensors with coordinates (i, j), (i, j - 1)
                    self.contract_((tag1, tag2), which='any')
                else:
                    # contract a specific pair
                    self.contract_between(tag1, (tag2, layer_tag))
            if canonize:
                #
                #   ──●       ──v
                #     ║         ║
                #   ──●  ==>  ──v
                #     ║         ║
                #   ──●       ──●
                #
                self.canonize_column(j, sweep=canonize_sweep, xrange=xrange)
            #
            #   ──v       ──●
            #     ║         │
            #   ──v  ==>  ──^
            #     ║         │
            #   ──●       ──^
            #
            self.compress_column(j, sweep=compress_sweep,
                                 xrange=xrange, **compress_opts)

    def _contract_boundary_from_right_multi(
        self,
        yrange,
        xrange,
        layer_tags,
        canonize=True,
        compress_sweep='down',
        **compress_opts
    ):
        for j in range(max(yrange), min(yrange), -1):
            # make sure the exterior sites are a single tensor
            #
            #         ──○──○           ──○──●
            #          ╱│ ╱│            ╱│ ╱│    (for two layer tags)
            #       ──○──○─●         ──○──●─╯
            #        ╱│╱╱│╱   ==>     ╱│╱╱│
            #     ──○──○─●         ──○──●─╯
            #       │╱ │╱            │╱ │
            #     ──●──●           ──●──╯
            #
            for i in range(min(xrange), max(xrange) + 1):
                self ^= (i, j)

            for tag in layer_tags:
                # contract interior sites from layer ``tag``
                #
                #         ──○
                #          ╱ ╲╲     (first contraction if there are two tags)
                #       ──○────v
                #        ╱ ╲╲ ╱
                #     ──○────v
                #        ╲╲ ╱
                #     ─────●
                #
                self._contract_boundary_from_right_single(
                    yrange=(j, j - 1), xrange=xrange, canonize=canonize,
                    compress_sweep=compress_sweep, layer_tag=tag,
                    **compress_opts)

                # so we can still uniqely identify 'inner' tensors, drop inner
                #     site tag merged into outer tensor for all but last tensor
                for i in range(min(xrange), max(xrange) + 1):
                    inner_tag = self.site_tag(i, j - 1)
                    if len(self.tag_map[inner_tag]) > 1:
                        self[i, j].drop_tags(inner_tag)

    def contract_boundary_from_right(
        self,
        yrange,
        xrange=None,
        canonize=True,
        compress_sweep='down',
        layer_tags=None,
        inplace=False,
        **compress_opts
    ):
        """Contract a 2D tensor network inwards from the left, canonizing and
        compressing (top to bottom) along the way.

        Parameters
        ----------
        yrange : (int, int)
            The range of columns to compress (inclusive).
        xrange : (int, int) or None, optional
            The range of rows to compress (inclusive), sweeping along with
            canonization and compression. Defaults to all rows.
        canonize : bool, optional
            Whether to sweep one way with canonization before compressing.
        compress_sweep : {'down', 'up'}, optional
            Which way to perform the compression sweep, which has an effect on
            which tensors end up being canonized.
        layer_tags : None or str, optional
            If ``None``, all tensors at each coordinate pair
            ``[(i, j), (i, j - 1)]`` will be first contracted. If specified,
            then the outer tensor at ``(i, j)`` will be contracted with the
            tensor specified by ``[(i + 1, j), layer_tag]``, for each
            ``layer_tag`` in ``layer_tags``.
        inplace : bool, optional
            Whether to perform the contraction inplace or not.
        compress_opts
            Supplied to
            :meth:`~quimb.tensor.tensor_2d.TensorNetwork2D.compress_column`.

        See Also
        --------
        contract_boundary_from_bottom, contract_boundary_from_top,
        contract_boundary_from_left
        """
        tn = self if inplace else self.copy()

        if xrange is None:
            xrange = (0, self.Lx - 1)

        if layer_tags is None:
            tn._contract_boundary_from_right_single(
                yrange, xrange, canonize=canonize,
                compress_sweep=compress_sweep, **compress_opts)
        else:
            tn._contract_boundary_from_right_multi(
                yrange, xrange, layer_tags, canonize=canonize,
                compress_sweep=compress_sweep, **compress_opts)

        return tn

    contract_boundary_from_right_ = functools.partialmethod(
        contract_boundary_from_right, inplace=True)

    def contract_boundary(
        self,
        around=None,
        layer_tags=None,
        max_separation=1,
        sequence=None,
        bottom=None,
        top=None,
        left=None,
        right=None,
        inplace=False,
        **boundary_contract_opts,
    ):
        """Contract the boundary of this 2D tensor network inwards::

            ●──●──●──●       ●──●──●──●       ●──●──●
            │  │  │  │       │  │  │  │       ║  │  │
            ●──●──●──●       ●──●──●──●       ^──●──●       >══>══●       >──v
            │  │ij│  │  ==>  │  │ij│  │  ==>  ║ij│  │  ==>  │ij│  │  ==>  │ij║
            ●──●──●──●       ●══<══<══<       ^──<──<       ^──<──<       ^──<
            │  │  │  │
            ●──●──●──●

        Optionally from any or all of the boundary, in multiple layers, and
        stopping around a region.

        Parameters
        ----------
        around : None or sequence of (int, int), optional
            If given, don't contract the square of sites bounding these
            coordinates.
        layer_tags : None or sequence of str, optional
            If given, perform a multilayer contraction, contracting the inner
            sites in each layer into the boundary individually.
        max_separation : int, optional
            If ``around is None``, when any two sides become this far apart
            simply contract the remaining tensor network.
        sequence : sequence of {'b', 'l', 't', 'r'}, optional
            Which directions to cycle throught when performing the inwards
            contractions: 'b', 'l', 't', 'r' corresponding to *from the*
            bottom, left, top and right respectively. If ``around`` is
            specified you will likely need all of these!
        bottom : int, optional
            The initial bottom boundary row, defaults to 0.
        top : int, optional
            The initial top boundary row, defaults to ``Lx - 1``.
        left : int, optional
            The initial left boundary column, defaults to 0.
        right : int, optional
            The initial right boundary column, defaults to ``Ly - 1``..
        inplace : bool, optional
            Whether to perform the contraction in place or not.
        boundary_contract_opts
            Supplied to
            :meth:`~quimb.tensor.tensor_2d.TensorNetwork2D.contract_boundary_from_bottom`,
            :meth:`~quimb.tensor.tensor_2d.TensorNetwork2D.contract_boundary_from_left`,
            :meth:`~quimb.tensor.tensor_2d.TensorNetwork2D.contract_boundary_from_top`,
            or
            :meth:`~quimb.tensor.tensor_2d.TensorNetwork2D.contract_boundary_from_right`,
            including compression and canonization options.
        """
        tn = self if inplace else self.copy()

        boundary_contract_opts['layer_tags'] = layer_tags

        # set default starting borders
        if bottom is None:
            bottom = 0
        if top is None:
            top = tn.Lx - 1
        if left is None:
            left = 0
        if right is None:
            right = tn.Ly - 1

        if around is not None:
            if sequence is None:
                sequence = 'bltr'
            stop_i_min = min(x[0] for x in around)
            stop_i_max = max(x[0] for x in around)
            stop_j_min = min(x[1] for x in around)
            stop_j_max = max(x[1] for x in around)
        elif sequence is None:
            # contract in along short dimension
            if self.Lx >= self.Ly:
                sequence = 'b'
            else:
                sequence = 'l'

        # keep track of whether we have hit the ``around`` region.
        reached_stop = {direction: False for direction in sequence}

        for direction in cycle(sequence):

            if direction == 'b':
                # for each direction check if we have reached the 'stop' region
                if (around is None) or (bottom + 1 < stop_i_min):
                    tn.contract_boundary_from_bottom_(
                        xrange=(bottom, bottom + 1),
                        yrange=(left, right),
                        compress_sweep='left',
                        **boundary_contract_opts,
                    )
                    bottom += 1
                else:
                    reached_stop[direction] = True

            elif direction == 'l':
                if (around is None) or (left + 1 < stop_j_min):
                    tn.contract_boundary_from_left_(
                        xrange=(bottom, top),
                        yrange=(left, left + 1),
                        compress_sweep='up',
                        **boundary_contract_opts
                    )
                    left += 1
                else:
                    reached_stop[direction] = True

            elif direction == 't':
                if (around is None) or (top - 1 > stop_i_max):
                    tn.contract_boundary_from_top_(
                        xrange=(top, top - 1),
                        compress_sweep='right',
                        yrange=(left, right),
                        **boundary_contract_opts
                    )
                    top -= 1
                else:
                    reached_stop[direction] = True

            elif direction == 'r':
                if (around is None) or (right - 1 > stop_j_max):
                    tn.contract_boundary_from_right_(
                        xrange=(bottom, top),
                        yrange=(right, right - 1),
                        compress_sweep='down',
                        **boundary_contract_opts
                    )
                    right -= 1
                else:
                    reached_stop[direction] = True

            else:
                raise ValueError("'sequence' should be an iterable of "
                                 "'b', 'l', 't', 'r' only.")

            if around is None:
                # check if TN has become thin enough to just contract
                thin_strip = (
                    (top - bottom <= max_separation) or
                    (right - left <= max_separation)
                )
                if thin_strip:
                    return tn.contract(all, optimize='auto-hq')

            # check if all directions have reached the ``around`` region
            elif all(reached_stop.values()):
                break

        return tn

    contract_boundary_ = functools.partialmethod(
        contract_boundary, inplace=True)

    def compute_row_environments(self, dense=False, **compress_opts):
        r"""Compute the ``2 * self.Lx`` 1D boundary tensor networks describing
        the lower and upper environments of each row in this 2D tensor network,
        *assumed to represent the norm*.

        The 'above' environment for row ``i`` will be a contraction of all
        rows ``i + 1, i + 2, ...`` etc::

             ●━━━●━━━●━━━●━━━●━━━●━━━●━━━●━━━●━━━●
            ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲

        The 'below' environment for row ``i`` will be a contraction of all
        rows ``i - 1, i - 2, ...`` etc::

            ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱
             ●━━━●━━━●━━━●━━━●━━━●━━━●━━━●━━━●━━━●

        Such that
        ``envs['above', i] & self.select(self.row_tag(i)) & envs['below', i]``
        would look like::

             ●━━━●━━━●━━━●━━━●━━━●━━━●━━━●━━━●━━━●
            ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲
            o─o─o─o─o─o─o─o─o─o─o─o─o─o─o─o─o─o─o─o
            ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱
             ●━━━●━━━●━━━●━━━●━━━●━━━●━━━●━━━●━━━●

        And be (an approximation of) the norm centered around row ``i``

        Parameters
        ----------
        dense : bool, optional
            If true, contract the boundary in as a single dense tensor.
        compress_opts
            Supplied to
            :meth:`~quimb.tensor.tensor_2d.TensorNetwork2D.contract_boundary_from_bottom`
            and
            :meth:`~quimb.tensor.tensor_2d.TensorNetwork2D.contract_boundary_from_top`
            .

        Returns
        -------
        row_envs : dict[(str, int), TensorNetwork]
            The two environment tensor networks of row ``i`` will be stored in
            ``row_envs['below', i]`` and ``row_envs['above', i]``.
        """
        row_envs = dict()

        # upwards pass
        row_envs['below', 0] = TensorNetwork([])
        first_row = self.row_tag(0)
        env_bottom = self.copy()
        if dense:
            env_bottom ^= first_row
        row_envs['below', 1] = env_bottom.select(first_row)
        for i in range(2, env_bottom.Lx):
            if dense:
                env_bottom ^= (self.row_tag(i - 2), self.row_tag(i - 1))
            else:
                env_bottom.contract_boundary_from_bottom_(
                    (i - 2, i - 1), **compress_opts)
            row_envs['below', i] = env_bottom.select(first_row)

        # downwards pass
        row_envs['above', self.Lx - 1] = TensorNetwork([])
        last_row = self.row_tag(self.Lx - 1)
        env_top = self.copy()
        if dense:
            env_top ^= last_row
        row_envs['above', self.Lx - 2] = env_top.select(last_row)
        for i in range(env_top.Lx - 3, -1, -1):
            if dense:
                env_top ^= (self.row_tag(i + 1), self.row_tag(i + 2))
            else:
                env_top.contract_boundary_from_top_(
                    (i + 1, i + 2), **compress_opts)
            row_envs['above', i] = env_top.select(last_row)

        return row_envs

    def compute_col_environments(self, dense=False, **compress_opts):
        r"""Compute the ``2 * self.Ly`` 1D boundary tensor networks describing
        the left and right environments of each column in this 2D tensor
        network, assumed to represent the norm.

        The 'left' environment for column ``j`` will be a contraction of all
        columns ``j - 1, j - 2, ...`` etc::

            ●<
            ┃
            ●<
            ┃
            ●<
            ┃
            ●<


        The 'right' environment for row ``j`` will be a contraction of all
        rows ``j + 1, j + 2, ...`` etc::

            >●
             ┃
            >●
             ┃
            >●
             ┃
            >●

        Such that
        ``envs['left', j] & self.select(self.col_tag(j)) & envs['right', j]``
        would look like::

               ╱o
            ●< o| >●
            ┃  |o  ┃
            ●< o| >●
            ┃  |o  ┃
            ●< o| >●
            ┃  |o  ┃
            ●< o╱ >●

        And be (an approximation of) the norm centered around column ``j``

        Parameters
        ----------
        dense : bool, optional
            If true, contract the boundary in as a single dense tensor.
        compress_opts
            Supplied to
            :meth:`~quimb.tensor.tensor_2d.TensorNetwork2D.contract_boundary_from_left`
            and
            :meth:`~quimb.tensor.tensor_2d.TensorNetwork2D.contract_boundary_from_right`
            .

        Returns
        -------
        col_envs : dict[(str, int), TensorNetwork]
            The two environment tensor networks of column ``j`` will be stored
            in ``row_envs['left', j]`` and ``row_envs['right', j]``.
        """
        col_envs = dict()

        # rightwards pass
        col_envs['left', 0] = TensorNetwork([])
        first_column = self.col_tag(0)
        env_right = self.copy()
        if dense:
            env_right ^= first_column
        col_envs['left', 1] = env_right.select(first_column)
        for j in range(2, env_right.Ly):
            if dense:
                env_right ^= (self.col_tag(j - 2), self.col_tag(j - 1))
            else:
                env_right.contract_boundary_from_left_(
                    (j - 2, j - 1), **compress_opts)
            col_envs['left', j] = env_right.select(first_column)

        # leftwards pass
        last_column = self.col_tag(self.Ly - 1)
        env_left = self.copy()
        col_envs['right', self.Ly - 1] = TensorNetwork([])
        col_envs['right', self.Ly - 2] = env_left.select(last_column)
        for j in range(self.Ly - 3, -1, -1):
            env_left.contract_boundary_from_right_(
                (j + 1, j + 2), **compress_opts)
            col_envs['right', j] = env_left.select(last_column)

        return col_envs

    def _compute_plaquette_environments_row_first(
        self,
        x_bsz,
        y_bsz,
        second_dense=None,
        row_envs=None,
        **compute_environment_opts
    ):
        if second_dense is None:
            second_dense = x_bsz < 2

        # first we contract from either side to produce column environments
        if row_envs is None:
            row_envs = self.compute_row_environments(
                **compute_environment_opts)

        # next we form vertical strips and contract from both top and bottom
        #     for each column
        col_envs = dict()
        for i in range(self.Lx - x_bsz + 1):
            #
            #      ●━━━●━━━●━━━●━━━●━━━●━━━●━━━●━━━●━━━●
            #     ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲
            #     o─o─o─o─o─o─o─o─o─o─o─o─o─o─o─o─o─o─o─o     ┬
            #     | | | | | | | | | | | | | | | | | | | |     ┊ x_bsz
            #     o─o─o─o─o─o─o─o─o─o─o─o─o─o─o─o─o─o─o─o     ┴
            #     ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱ ╲ ╱
            #      ●━━━●━━━●━━━●━━━●━━━●━━━●━━━●━━━●━━━●
            #
            row_i = TensorNetwork((
                row_envs['below', i],
                self.select_any([self.row_tag(i + x) for x in range(x_bsz)]),
                row_envs['above', i + x_bsz - 1],
            ), check_collisions=False).view_as_(TensorNetwork2D, like=self)
            #
            #           y_bsz
            #           <-->               second_dense=True
            #       ●──      ──●
            #       │          │            ╭──     ──╮
            #       ●── .  . ──●            │╭─ . . ─╮│     ┬
            #       │          │     or     ●         ●     ┊ x_bsz
            #       ●── .  . ──●            │╰─ . . ─╯│     ┴
            #       │          │            ╰──     ──╯
            #       ●──      ──●
            #     'left'    'right'       'left'    'right'
            #
            col_envs[i] = row_i.compute_col_environments(
                xrange=(max(i - 1, 0), min(i + x_bsz, self.Lx - 1)),
                dense=second_dense, **compute_environment_opts)

        # then range through all the possible plaquettes, selecting the correct
        # boundary tensors from either the column or row environments
        plaquette_envs = dict()
        for i0, j0 in product(range(self.Lx - x_bsz + 1),
                              range(self.Ly - y_bsz + 1)):

            # we want to select bordering tensors from:
            #
            #       L──A──A──R    <- A from the row environments
            #       │  │  │  │
            #  i0+1 L──●──●──R
            #       │  │  │  │    <- L, R from the column environments
            #  i0   L──●──●──R
            #       │  │  │  │
            #       L──B──B──R    <- B from the row environments
            #
            #         j0  j0+1
            #
            left_coos = ((i0 + x, j0 - 1) for x in range(-1, x_bsz + 1))
            left_tags = tuple(
                starmap(self.site_tag, filter(self.valid_coo, left_coos)))

            right_coos = ((i0 + x, j0 + y_bsz) for x in range(-1, x_bsz + 1))
            right_tags = tuple(
                starmap(self.site_tag, filter(self.valid_coo, right_coos)))

            below_coos = ((i0 - 1, j0 + x) for x in range(y_bsz))
            below_tags = tuple(
                starmap(self.site_tag, filter(self.valid_coo, below_coos)))

            above_coos = ((i0 + x_bsz, j0 + x) for x in range(y_bsz))
            above_tags = tuple(
                starmap(self.site_tag, filter(self.valid_coo, above_coos)))

            env_ij = TensorNetwork((
                col_envs[i0]['left', j0].select_any(left_tags),
                col_envs[i0]['right', j0 + y_bsz - 1].select_any(right_tags),
                row_envs['below', i0].select_any(below_tags),
                row_envs['above', i0 + x_bsz - 1].select_any(above_tags),
            ), check_collisions=False)

            # finally, absorb any rank-2 corner tensors
            env_ij.rank_simplify_()

            plaquette_envs[(i0, j0), (x_bsz, y_bsz)] = env_ij

        return plaquette_envs

    def _compute_plaquette_environments_col_first(
        self,
        x_bsz,
        y_bsz,
        second_dense=None,
        col_envs=None,
        **compute_environment_opts
    ):
        if second_dense is None:
            second_dense = y_bsz < 2

        # first we contract from either side to produce column environments
        if col_envs is None:
            col_envs = self.compute_col_environments(
                **compute_environment_opts)

        # next we form vertical strips and contract from both top and bottom
        #     for each column
        row_envs = dict()
        for j in range(self.Ly - y_bsz + 1):
            #
            #        y_bsz
            #        <-->
            #
            #      ╭─╱o─╱o─╮
            #     ●──o|─o|──●
            #     ┃╭─|o─|o─╮┃
            #     ●──o|─o|──●
            #     ┃╭─|o─|o─╮┃
            #     ●──o|─o|──●
            #     ┃╭─|o─|o─╮┃
            #     ●──o╱─o╱──●
            #     ┃╭─|o─|o─╮┃
            #     ●──o╱─o╱──●
            #
            col_j = TensorNetwork((
                col_envs['left', j],
                self.select_any([self.col_tag(j + jn) for jn in range(y_bsz)]),
                col_envs['right', j + y_bsz - 1],
            ), check_collisions=False).view_as_(TensorNetwork2D, like=self)
            #
            #        y_bsz
            #        <-->        second_dense=True
            #     ●──●──●──●      ╭──●──╮
            #     │  │  │  │  or  │ ╱ ╲ │    'above'
            #        .  .           . .                  ┬
            #                                            ┊ x_bsz
            #        .  .           . .                  ┴
            #     │  │  │  │  or  │ ╲ ╱ │    'below'
            #     ●──●──●──●      ╰──●──╯
            #
            row_envs[j] = col_j.compute_row_environments(
                yrange=(max(j - 1, 0), min(j + y_bsz, self.Ly - 1)),
                dense=second_dense, **compute_environment_opts)

        # then range through all the possible plaquettes, selecting the correct
        # boundary tensors from either the column or row environments
        plaquette_envs = dict()
        for i0, j0 in product(range(self.Lx - x_bsz + 1),
                              range(self.Ly - y_bsz + 1)):

            # we want to select bordering tensors from:
            #
            #          A──A──A──A    <- A from the row environments
            #          │  │  │  │
            #     i0+1 L──●──●──R
            #          │  │  │  │    <- L, R from the column environments
            #     i0   L──●──●──R
            #          │  │  │  │
            #          B──B──B──B    <- B from the row environments
            #
            #            j0  j0+1
            #
            left_coos = ((i0 + x, j0 - 1) for x in range(x_bsz))
            left_tags = tuple(
                starmap(self.site_tag, filter(self.valid_coo, left_coos)))

            right_coos = ((i0 + x, j0 + y_bsz) for x in range(x_bsz))
            right_tags = tuple(
                starmap(self.site_tag, filter(self.valid_coo, right_coos)))

            below_coos = ((i0 - 1, j0 + x) for x in range(- 1, y_bsz + 1))
            below_tags = tuple(
                starmap(self.site_tag, filter(self.valid_coo, below_coos)))

            above_coos = ((i0 + x_bsz, j0 + x) for x in range(- 1, y_bsz + 1))
            above_tags = tuple(
                starmap(self.site_tag, filter(self.valid_coo, above_coos)))

            env_ij = TensorNetwork((
                col_envs['left', j0].select_any(left_tags),
                col_envs['right', j0 + y_bsz - 1].select_any(right_tags),
                row_envs[j0]['below', i0].select_any(below_tags),
                row_envs[j0]['above', i0 + x_bsz - 1].select_any(above_tags),
            ), check_collisions=False)

            # finally, absorb any rank-2 corner tensors
            env_ij.rank_simplify_()

            plaquette_envs[(i0, j0), (x_bsz, y_bsz)] = env_ij

        return plaquette_envs

    def compute_plaquette_environments(
        self,
        x_bsz=2,
        y_bsz=2,
        first_contract=None,
        second_dense=None,
        **compute_environment_opts,
    ):
        r"""Compute all environments like::

            second_dense=False   second_dense=True (& first_contract='columns')

              ●──●                  ╭───●───╮
             ╱│  │╲                 │  ╱ ╲  │
            ●─.  .─●    ┬           ●─ . . ─●    ┬
            │      │    ┊ x_bsz     │       │    ┊ x_bsz
            ●─.  .─●    ┴           ●─ . . ─●    ┴
             ╲│  │╱                 │  ╲ ╱  │
              ●──●                  ╰───●───╯

              <-->                    <->
             y_bsz                   y_bsz

        Use two boundary contractions sweeps.

        Parameters
        ----------
        x_bsz : int, optional
            The size of the plaquettes in the x-direction (number of rows).
        y_bsz : int, optional
            The size of the plaquettes in the y-direction (number of columns).
        first_contract : {None, 'rows', 'columns'}, optional
            The environments can either be generated with initial sweeps in
            the row or column direction. Generally it makes sense to perform
            this approximate step in whichever is smaller (the default).
        second_dense : None or bool, optional
            Whether to perform the second set of contraction sweeps (in the
            rotated direction from whichever ``first_contract`` is) using
            a dense tensor or boundary method.
        compute_environment_opts
            Supplied to
            :meth:`~quimb.tensor.tensor_2d.TensorNetwork2D.compute_col_environments`
            or
            :meth:`~quimb.tensor.tensor_2d.TensorNetwork2D.compute_row_environments`
            .

        Returns
        -------
        dict[((int, int), (int, int)), TensorNetwork]
            The plaquette environments. The key is two tuples of ints, the
            startings coordinate of the plaquette being the first and the size
            of the plaquette being the second pair.
        """
        if first_contract is None:
            if x_bsz > y_bsz:
                first_contract = 'columns'
            elif y_bsz > x_bsz:
                first_contract = 'rows'
            elif self.Lx >= self.Ly:
                first_contract = 'rows'
            else:
                first_contract = 'columns'

        compute_env_fn = {
            'rows': self._compute_plaquette_environments_row_first,
            'columns': self._compute_plaquette_environments_col_first,
        }[first_contract]

        return compute_env_fn(
            x_bsz=x_bsz, y_bsz=y_bsz, second_dense=second_dense,
            **compute_environment_opts)


def is_lone_coo(where):
    """Check if ``where`` has been specified as a single coordinate pair.
    """
    return (len(where) == 2) and (isinstance(where[0], Integral))


def gate_string_split_(TG, where, string, original_ts, bonds_along,
                       reindex_map, site_ix, info, **compress_opts):

    # by default this means singuvalues are kept in the string 'blob' tensor
    compress_opts.setdefault('absorb', 'right')

    # the outer, neighboring indices of each tensor in the string
    neighb_inds = []

    # tensors we are going to contract in the blob, reindex some to attach gate
    contract_ts = []

    for t, coo in zip(original_ts, string):
        neighb_inds.append(tuple(ix for ix in t.inds if ix not in bonds_along))
        contract_ts.append(t.reindex(reindex_map) if coo in where else t)

    # form the central blob of all sites and gate contracted
    blob = tensor_contract(*contract_ts, TG)

    regauged = []

    # one by one extract the site tensors again from each end
    inner_ts = [None] * len(string)
    i = 0
    j = len(string) - 1

    while True:
        lix = neighb_inds[i]
        if i > 0:
            lix += (bonds_along[i - 1],)

        # the original bond we are restoring
        bix = bonds_along[i]

        # split the blob!
        inner_ts[i], *maybe_svals, blob = blob.split(
            left_inds=lix, get='tensors', bond_ind=bix, **compress_opts)

        # if singular values are returned (``absorb=None``) check if we should
        #     return them via ``info``, e.g. for ``SimpleUpdate`
        if maybe_svals and info is not None:
            s = next(iter(maybe_svals)).data
            coo_pair = tuple(sorted((string[i], string[i + 1])))
            info['singular_values', coo_pair] = s

            # regauge the blob but record so as to unguage later
            if i != j - 1:
                blob.multiply_index_diagonal_(bix, s)
                regauged.append((i + 1, bix, s))

        # move inwards along string, terminate if two ends meet
        i += 1
        if i == j:
            inner_ts[i] = blob
            break

        # extract at end of string
        lix = neighb_inds[j]
        if j < len(string) - 1:
            lix += (bonds_along[j],)

        # the original bond we are restoring
        bix = bonds_along[j - 1]

        # split the blob!
        inner_ts[j], *maybe_svals, blob = blob.split(
            left_inds=lix, get='tensors', bond_ind=bix, **compress_opts)

        # if singular values are returned (``absorb=None``) check if we should
        #     return them via ``info``, e.g. for ``SimpleUpdate`
        if maybe_svals and info is not None:
            s = next(iter(maybe_svals)).data
            coo_pair = tuple(sorted((string[j - 1], string[j])))
            info['singular_values', coo_pair] = s

            # regauge the blob but record so as to unguage later
            if j != i + 1:
                blob.multiply_index_diagonal_(bix, s)
                regauged.append((j - 1, bix, s))

        # move inwards along string, terminate if two ends meet
        j -= 1
        if j == i:
            inner_ts[j] = blob
            break

    # ungauge the site tensors along bond if necessary
    for i, bix, s in regauged:
        t = inner_ts[i]
        t.multiply_index_diagonal_(bix, s**-1)

    # transpose to match original tensors and update original data
    for to, tn in zip(original_ts, inner_ts):
        tn.transpose_like_(to)
        to.modify(data=tn.data)


def gate_string_reduce_split_(TG, where, string, original_ts, bonds_along,
                              reindex_map, site_ix, info, **compress_opts):

    # by default this means singuvalues are kept in the string 'blob' tensor
    compress_opts.setdefault('absorb', 'right')

    # indices to reduce, first and final include physical indices for gate
    inds_to_reduce = [(bonds_along[0], site_ix[0])]
    for b1, b2 in pairwise(bonds_along):
        inds_to_reduce.append((b1, b2))
    inds_to_reduce.append((bonds_along[-1], site_ix[-1]))

    # tensors that remain on the string sites and those pulled into string
    outer_ts, inner_ts = [], []
    for coo, rix, t in zip(string, inds_to_reduce, original_ts):
        tq, tr = t.split(left_inds=None, right_inds=rix,
                         method='qr', get='tensors')
        outer_ts.append(tq)
        inner_ts.append(tr.reindex_(reindex_map) if coo in where else tr)

    # contract the blob of gate with reduced tensors only
    blob = tensor_contract(*inner_ts, TG)

    regauged = []

    # extract the new reduced tensors sequentially from each end
    i = 0
    j = len(string) - 1

    while True:

        # extract at beginning of string
        lix = bonds(blob, outer_ts[i])
        if i == 0:
            lix.add(site_ix[0])
        else:
            lix.add(bonds_along[i - 1])

        # the original bond we are restoring
        bix = bonds_along[i]

        # split the blob!
        inner_ts[i], *maybe_svals, blob = blob.split(
            left_inds=lix, get='tensors', bond_ind=bix, **compress_opts)

        # if singular values are returned (``absorb=None``) check if we should
        #     return them via ``info``, e.g. for ``SimpleUpdate`
        if maybe_svals and info is not None:
            s = next(iter(maybe_svals)).data
            coo_pair = tuple(sorted((string[i], string[i + 1])))
            info['singular_values', coo_pair] = s

            # regauge the blob but record so as to unguage later
            if i != j - 1:
                blob.multiply_index_diagonal_(bix, s)
                regauged.append((i + 1, bix, s))

        # move inwards along string, terminate if two ends meet
        i += 1
        if i == j:
            inner_ts[i] = blob
            break

        # extract at end of string
        lix = bonds(blob, outer_ts[j])
        if j == len(string) - 1:
            lix.add(site_ix[-1])
        else:
            lix.add(bonds_along[j])

        # the original bond we are restoring
        bix = bonds_along[j - 1]

        # split the blob!
        inner_ts[j], *maybe_svals, blob = blob.split(
            left_inds=lix, get='tensors', bond_ind=bix, **compress_opts)

        # if singular values are returned (``absorb=None``) check if we should
        #     return them via ``info``, e.g. for ``SimpleUpdate`
        if maybe_svals and info is not None:
            s = next(iter(maybe_svals)).data
            coo_pair = tuple(sorted((string[j - 1], string[j])))
            info['singular_values', coo_pair] = s

            # regauge the blob but record so as to unguage later
            if j != i + 1:
                blob.multiply_index_diagonal_(bix, s)
                regauged.append((j - 1, bix, s))

        # move inwards along string, terminate if two ends meet
        j -= 1
        if j == i:
            inner_ts[j] = blob
            break

    # reabsorb the inner reduced tensors into the sites
    new_ts = [
        tensor_contract(ts, tr, output_inds=to.inds)
        for to, ts, tr in zip(original_ts, outer_ts, inner_ts)
    ]

    # ungauge the site tensors along bond if necessary
    for i, bix, s in regauged:
        t = new_ts[i]
        t.multiply_index_diagonal_(bix, s**-1)

    # update originals
    for to, t in zip(original_ts, new_ts):
        to.modify(data=t.data)


class TensorNetwork2DVector(TensorNetwork2D,
                            TensorNetwork):
    """Mixin class  for a 2D square lattice vector TN, i.e. one with a single
    physical index per site.
    """

    _EXTRA_PROPS = (
        '_site_tag_id',
        '_row_tag_id',
        '_col_tag_id',
        '_Lx',
        '_Ly',
        '_site_ind_id',
    )

    @property
    def site_ind_id(self):
        return self._site_ind_id

    def site_ind(self, i, j):
        if not isinstance(i, str):
            i = i % self.Lx
        if not isinstance(j, str):
            j = j % self.Ly
        return self.site_ind_id.format(i, j)

    def reindex_sites(self, new_id, where=None, inplace=False):
        if where is None:
            where = self.gen_site_coos()

        return self.reindex(
            {
                self.site_ind(*ij): new_id.format(*ij) for ij in where
            },
            inplace=inplace
        )

    @site_ind_id.setter
    def site_ind_id(self, new_id):
        if self._site_ind_id != new_id:
            self.reindex_sites(new_id, inplace=True)
            self._site_ind_id = new_id

    @property
    def site_inds(self):
        """All of the site inds.
        """
        return tuple(starmap(self.site_ind, self.gen_site_coos()))

    def to_dense(self, *inds_seq, **contract_opts):
        """Return the dense ket version of this 2D vector, i.e. a ``qarray``
        with shape (-1, 1).
        """
        if not inds_seq:
            # just use list of site indices
            return do('reshape', TensorNetwork.to_dense(
                self, self.site_inds, **contract_opts
            ), (-1, 1))

        return TensorNetwork.to_dense(self, *inds_seq, **contract_opts)

    def phys_dim(self, i=None, j=None):
        """Get the size of the physical indices / a specific physical index.
        """
        if (i is not None) and (j is not None):
            pix = self.site_ind(i, j)
        else:
            # allow for when some physical indices might have been contracted
            pix = next(iter(
                ix for ix in self.site_inds if ix in self.ind_map
            ))
        return self.ind_size(pix)

    def make_norm(
        self,
        mangle_append='*',
        layer_tags=('KET', 'BRA'),
        return_all=False,
    ):
        """Make the norm tensor network of this 2D vector.

        Parameters
        ----------
        mangle_append : {str, False or None}, optional
            How to mangle the inner indices of the bra.
        layer_tags : (str, str), optional
            The tags to identify the top and bottom.
        return_all : bool, optional
            Return the norm, the ket and the bra.
        """
        ket = self.copy()
        ket.add_tag(layer_tags[0])

        bra = ket.retag({layer_tags[0]: layer_tags[1]})
        bra.conj_(mangle_append)

        norm = ket | bra

        if return_all:
            return norm, ket, bra
        return norm

    def gate(
        self,
        G,
        where,
        contract=False,
        tags=None,
        inplace=False,
        info=None,
        long_range_use_swaps=False,
        long_range_path_sequence=None,
        **compress_opts
    ):
        """Apply the dense gate ``G``, maintaining the physical indices of this
        2D vector tensor network.

        Parameters
        ----------
        G : array_like
            The gate array to apply, should match or be factorable into the
            shape ``(phys_dim,) * (2 * len(where))``.
        where : sequence of tuple[int, int] or tuple[int, int]
            Which site coordinates to apply the gate to.
        contract : {'reduce-split', 'split', False, True}, optional
            How to contract the gate into the 2D tensor network:

                - False: gate is added to network and nothing is contracted,
                  tensor network structure is thus not maintained.
                - True: gate is contracted with all tensors involved, tensor
                  network structure is thus only maintained if gate acts on a
                  single site only.
                - 'split': contract all involved tensors then split the result
                  back into two.
                - 'reduce-split': factor the two physical indices into
                  'R-factors' using QR decompositions on the original site
                  tensors, then contract the gate, split it and reabsorb each
                  side. Much cheaper than ``'split'``.

            The final three methods are relevant for two site gates only, for
            single site gates they use the ``contract=True`` option which also
            maintains the structure of the TN. See below for a pictorial
            description of each method.
        tags : str or sequence of str, optional
            Tags to add to the new gate tensor.
        inplace : bool, optional
            Whether to perform the gate operation inplace on the tensor
            network or not.
        info : None or dict, optional
            Used to store extra optional information such as the singular
            values if not absorbed.
        compress_opts
            Supplied to :func:`~quimb.tensor.tensor_core.tensor_split` for any
            ``contract`` methods that involve splitting. Ignored otherwise.

        Returns
        -------
        G_psi : TensorNetwork2DVector
            The new 2D vector TN like ``IIIGII @ psi`` etc.

        Notes
        -----

        The ``contract`` options look like the following (for two site gates).

        ``contract=False``::

              │   │
              GGGGG
              │╱  │╱
            ──●───●──
             ╱   ╱

        ``contract=True``::

              │╱  │╱
            ──GGGGG──
             ╱   ╱

        ``contract='split'``::

              │╱  │╱          │╱  │╱
            ──GGGGG──  ==>  ──G┄┄┄G──
             ╱   ╱           ╱   ╱
             <SVD>

        ``contract='reduce-split'``::

               │   │             │ │
               GGGGG             GGG               │ │
               │╱  │╱   ==>     ╱│ │  ╱   ==>     ╱│ │  ╱          │╱  │╱
             ──●───●──       ──>─●─●─<──       ──>─GGG─<──  ==>  ──G┄┄┄G──
              ╱   ╱           ╱     ╱           ╱     ╱           ╱   ╱
            <QR> <LQ>                            <SVD>

        For one site gates when one of the 'split' methods is supplied
        ``contract=True`` is assumed.
        """
        check_opt("contract", contract, (False, True, 'split', 'reduce-split'))

        psi = self if inplace else self.copy()

        if is_lone_coo(where):
            where = (where,)
        else:
            where = tuple(where)
        ng = len(where)

        dp = psi.phys_dim(*where[0])
        tags = tags_to_oset(tags)

        # allow a matrix to be reshaped into a tensor if it factorizes
        #     i.e. (4, 4) assumed to be two qubit gate -> (2, 2, 2, 2)
        G = maybe_factor_gate_into_tensor(G, dp, ng, where)

        site_ix = [psi.site_ind(i, j) for i, j in where]
        # new indices to join old physical sites to new gate
        bnds = [rand_uuid() for _ in range(ng)]
        reindex_map = dict(zip(site_ix, bnds))

        TG = Tensor(G, inds=site_ix + bnds, tags=tags, left_inds=bnds)

        if contract is False:
            #
            #       │   │      <- site_ix
            #       GGGGG
            #       │╱  │╱     <- bnds
            #     ──●───●──
            #      ╱   ╱
            #
            psi.reindex_(reindex_map)
            psi |= TG
            return psi

        elif (contract is True) or (ng == 1):
            #
            #       │╱  │╱
            #     ──GGGGG──
            #      ╱   ╱
            #
            psi.reindex_(reindex_map)

            # get the sites that used to have the physical indices
            site_tids = psi._get_tids_from_inds(bnds, which='any')

            # pop the sites, contract, then re-add
            pts = [psi._pop_tensor(tid) for tid in site_tids]
            psi |= tensor_contract(*pts, TG)

            return psi

        # following are all based on splitting tensors to maintain structure
        ij_a, ij_b = where

        # parse the argument specifying how to find the path between
        # non-nearest neighbours
        if long_range_path_sequence is not None:
            # make sure we can index
            long_range_path_sequence = tuple(long_range_path_sequence)
            # if the first element is a str specifying move sequence, e.g.
            #     ('v', 'h')
            #     ('av', 'bv', 'ah', 'bh')  # using swaps
            manual_lr_path = not isinstance(long_range_path_sequence[0], str)
            # otherwise assume a path has been manually specified, e.g.
            #     ((1, 2), (2, 2), (2, 3), ... )
            #     (((1, 1), (1, 2)), ((4, 3), (3, 3)), ...)  # using swaps
        else:
            manual_lr_path = False

        # check if we are not nearest neighbour and need to swap first
        if long_range_use_swaps:

            if manual_lr_path:
                *swaps, final = long_range_path_sequence
            else:
                # find a swap path
                *swaps, final = gen_long_range_swap_path(
                    ij_a, ij_b, sequence=long_range_path_sequence)

            # move the sites together
            SWAP = get_swap(dp, dtype=get_dtype_name(G),
                            backend=infer_backend(G))
            for pair in swaps:
                psi.gate_(SWAP, pair, contract=contract, absorb='right')

            compress_opts['info'] = info
            compress_opts['contract'] = contract

            # perform actual gate also compressing etc on 'way back'
            psi.gate_(G, final, **compress_opts)

            compress_opts.setdefault('absorb', 'both')
            for pair in reversed(swaps):
                psi.gate_(SWAP, pair, **compress_opts)

            return psi

        if manual_lr_path:
            string = long_range_path_sequence
        else:
            string = tuple(gen_long_range_path(
                *where, sequence=long_range_path_sequence))

        # the tensors along this string, which will be updated
        original_ts = [psi[coo] for coo in string]

        # the len(string) - 1 indices connecting the string
        bonds_along = [next(iter(bonds(t1, t2)))
                       for t1, t2 in pairwise(original_ts)]

        if contract == 'split':
            #
            #       │╱  │╱          │╱  │╱
            #     ──GGGGG──  ==>  ──G┄┄┄G──
            #      ╱   ╱           ╱   ╱
            #
            gate_string_split_(
                TG, where, string, original_ts, bonds_along,
                reindex_map, site_ix, info, **compress_opts)

        elif contract == 'reduce-split':
            #
            #       │   │             │ │
            #       GGGGG             GGG               │ │
            #       │╱  │╱   ==>     ╱│ │  ╱   ==>     ╱│ │  ╱          │╱  │╱
            #     ──●───●──       ──>─●─●─<──       ──>─GGG─<──  ==>  ──G┄┄┄G──
            #      ╱   ╱           ╱     ╱           ╱     ╱           ╱   ╱
            #    <QR> <LQ>                            <SVD>
            #
            gate_string_reduce_split_(
                TG, where, string, original_ts, bonds_along,
                reindex_map, site_ix, info, **compress_opts)

        return psi

    gate_ = functools.partialmethod(gate, inplace=True)

    def compute_norm(
        self,
        layer_tags=('KET', 'BRA'),
        **contract_opts,
    ):
        """Compute the norm of this vector via boundary contraction.
        """
        norm = self.make_norm(layer_tags=layer_tags)
        return norm.contract_boundary(layer_tags=layer_tags, **contract_opts)

    def compute_local_expectation(
        self,
        terms,
        normalized=False,
        autogroup=True,
        contract_optimize='auto-hq',
        return_all=False,
        plaquette_envs=None,
        plaquette_map=None,
        **plaquette_env_options,
    ):
        r"""Compute the sum of many local expecations by essentially forming
        the reduced density matrix of all required plaquettes.

        Parameters
        ----------
        terms : dict[tuple[tuple[int], array]
            A dictionary mapping site coordinates to raw operators, which will
            be supplied to
            :meth:`~quimb.tensor.tensor_2d.TensorNetwork2DVector.gate`.
        normalized : bool, optional
            If True, normalize the value of each local expectation by the local
            norm: $\langle O_i \rangle = Tr[\rho_p O_i] / Tr[\rho_p]$.
        autogroup : bool, optional
            If ``True`` (the default), group terms into horizontal and vertical
            sets to be computed separately (usually more efficient) if
            possible.
        contract_optimize : str, optional
            Contraction path finder to use for contracting the local plaquette
            expectation (and optionally normalization).
        return_all : bool, optional
            Whether to the return all the values individually as a dictionary
            of coordinates to tuple[local_expectation, local_norm].
        plaquette_envs : None or dict, optional
            Supply precomputed plaquette environments.
        plaquette_map : None, dict, optional
            Supply the mapping of which plaquettes (denoted by
            ``((x0, y0), (dx, dy))``) to use for which coordinates, it will be
            calculated automatically otherwise.
        plaquette_env_options
            Supplied to
            :meth:`~quimb.tensor.tensor_2d.TensorNetwork2D.compute_plaquette_environments`
            to generate the plaquette environments, equivalent to approximately
            performing the partial trace.

        Returns
        -------
        scalar or dict
        """
        norm, ket, bra = self.make_norm(return_all=True)

        if plaquette_envs is None:
            # set some sensible defaults
            plaquette_env_options.setdefault('layer_tags', ('KET', 'BRA'))

            plaquette_envs = dict()
            for x_bsz, y_bsz in calc_plaquette_sizes(terms, autogroup):
                plaquette_envs.update(norm.compute_plaquette_environments(
                    x_bsz=x_bsz, y_bsz=y_bsz, **plaquette_env_options))

        if plaquette_map is None:
            # work out which plaquettes to use for which terms
            plaquette_map = calc_plaquette_map(plaquette_envs)

        # now group the terms into just the plaquettes we need
        plaq2coo = defaultdict(list)
        for where, G in terms.items():
            p = plaquette_map[where]
            plaq2coo[p].append((where, G))

        expecs = dict()
        for p in plaq2coo:
            # site tags for the plaquette
            sites = tuple(starmap(ket.site_tag, plaquette_to_sites(p)))

            # view the ket portion as 2d vector so we can gate it
            ket_local = ket.select_any(sites)
            ket_local.view_as_(TensorNetwork2DVector, like=self)
            bra_and_env = bra.select_any(sites) | plaquette_envs[p]

            with oe.shared_intermediates():
                # compute local estimation of norm for this plaquette
                if normalized:
                    norm_i0j0 = (
                        ket_local | bra_and_env
                    ).contract(all, optimize=contract_optimize)
                else:
                    norm_i0j0 = None

                # for each local term on plaquette compute expectation
                for where, G in plaq2coo[p]:
                    expec_ij = (
                        ket_local.gate(G, where, contract=False) | bra_and_env
                    ).contract(all, optimize=contract_optimize)

                    expecs[where] = expec_ij, norm_i0j0

        if return_all:
            return expecs

        if normalized:
            return functools.reduce(add, (e / n for e, n in expecs.values()))

        return functools.reduce(add, (e for e, _ in expecs.values()))

    def normalize(
        self,
        balance_bonds=False,
        equalize_norms=False,
        inplace=False,
        **boundary_contract_opts,
    ):
        """Normalize this PEPS.

        Parameters
        ----------
        inplace : bool, optional
            Whether to perform the normalization inplace or not.
        balance_bonds : bool, optional
            Whether to balance the bonds after normalization, a form of
            conditioning.
        equalize_norms : bool, optional
            Whether to set all the tensor norms to the same value after
            normalization, another form of conditioning.
        boundary_contract_opts
            Supplied to
            :meth:`~quimb.tensor.tensor_2d.TensorNetwork2D.contract_boundary`,
            by default, two layer contraction will be used.
        """
        norm = self.make_norm()

        # default to two layer contraction
        boundary_contract_opts.setdefault('layer_tags', ('KET', 'BRA'))

        nfact = norm.contract_boundary(**boundary_contract_opts)

        n_ket = self.multiply_each(
            nfact**(-1 / (2 * self.num_tensors)), inplace=inplace)

        if balance_bonds:
            n_ket.balance_bonds_()

        if equalize_norms:
            n_ket.equalize_norms_()

        return n_ket

    normalize_ = functools.partialmethod(normalize, inplace=True)


class TensorNetwork2DOperator(TensorNetwork2D,
                              TensorNetwork):
    """Mixin class  for a 2D square lattice TN operator, i.e. one with both
    'upper' and 'lower' site (physical) indices.
    """

    _EXTRA_PROPS = (
        '_site_tag_id',
        '_row_tag_id',
        '_col_tag_id',
        '_Lx',
        '_Ly',
        '_upper_ind_id',
        '_lower_ind_id',
    )

    @property
    def lower_ind_id(self):
        return self._lower_ind_id

    def lower_ind(self, i, j):
        if not isinstance(i, str):
            i = i % self.Lx
        if not isinstance(j, str):
            j = j % self.Ly
        return self.lower_ind_id.format(i, j)

    @property
    def lower_inds(self):
        """All of the lower inds.
        """
        return tuple(starmap(self.lower_ind, self.gen_site_coos()))

    @property
    def upper_ind_id(self):
        return self._upper_ind_id

    def upper_ind(self, i, j):
        if not isinstance(i, str):
            i = i % self.Lx
        if not isinstance(j, str):
            j = j % self.Ly
        return self.upper_ind_id.format(i, j)

    @property
    def upper_inds(self):
        """All of the upper inds.
        """
        return tuple(starmap(self.upper_ind, self.gen_site_coos()))

    def to_dense(self, *inds_seq, **contract_opts):
        """Return the dense matrix version of this 2D operator, i.e. a
        ``qarray`` with shape (d, d).
        """
        if not inds_seq:
            inds_seq = (self.upper_inds, self.lower_inds)

        return TensorNetwork.to_dense(self, *inds_seq, **contract_opts)

    def phys_dim(self, i=0, j=0, which='upper'):
        """Get a physical index size of this 2D operator.
        """
        if which == 'upper':
            return self[i, j].ind_size(self.upper_ind(i, j))

        if which == 'lower':
            return self[i, j].ind_size(self.lower_ind(i, j))


class TensorNetwork2DFlat(TensorNetwork2D,
                          TensorNetwork):
    """Mixin class for a 2D square lattice tensor network with a single tensor
    per site, for example, both PEPS and PEPOs.
    """

    _EXTRA_PROPS = (
        '_site_tag_id',
        '_row_tag_id',
        '_col_tag_id',
        '_Lx',
        '_Ly',
    )

    def bond(self, coo1, coo2):
        """Get the name of the index defining the bond between sites at
        ``coo1`` and ``coo2``.
        """
        b_ix, = self[coo1].bonds(self[coo2])
        return b_ix

    def bond_size(self, coo1, coo2):
        """Return the size of the bond between sites at ``coo1`` and ``coo2``.
        """
        b_ix = self.bond(coo1, coo2)
        return self[coo1].ind_size(b_ix)

    def expand_bond_dimension(self, new_bond_dim, inplace=True, bra=None,
                              rand_strength=0.0):
        """Increase the bond dimension of this flat, 2D, tensor network,
        padding the tensor data with either zeros or random entries.

        Parameters
        ----------
        new_bond_dim : int
            The new dimension. If smaller or equal to the current bond
            dimension nothing will happend.
        inplace : bool, optional
            Whether to expand in place (the default), or return a new TN.
        bra : TensorNetwork2DFlat, optional
            Expand this TN with the same data also, assuming it to be the
            conjugate, bra, TN.
        rand_strength : float, optional
            If greater than zero, pad the data arrays with gaussian noise of
            this strength.

        Returns
        -------
        expanded : TensorNetwork2DFlat
        """

        expanded = self if inplace else self.copy()

        for coo_a in self.gen_site_coos():
            tensor = expanded[coo_a]
            inds_to_expand = [
                self.bond(coo_a, coo_b)
                for coo_b in nearest_neighbors(coo_a)
                if self.valid_coo(coo_b)
            ]

            pads = [(0, 0) if i not in inds_to_expand else
                    (0, max(new_bond_dim - d, 0))
                    for d, i in zip(tensor.shape, tensor.inds)]

            if rand_strength > 0:
                edata = do('pad', tensor.data, pads, mode=rand_padder,
                           rand_strength=rand_strength)
            else:
                edata = do('pad', tensor.data, pads, mode='constant')

            tensor.modify(data=edata)

            if bra is not None:
                bra[coo_a].modify(data=tensor.data.conj())

        return expanded


class PEPS(TensorNetwork2DVector,
           TensorNetwork2DFlat,
           TensorNetwork2D,
           TensorNetwork):
    r"""Projected Entangled Pair States object::


                         ...
             │    │    │    │    │    │
             ●────●────●────●────●────●──
            ╱│   ╱│   ╱│   ╱│   ╱│   ╱│
             │    │    │    │    │    │
             ●────●────●────●────●────●──
            ╱│   ╱│   ╱│   ╱│   ╱│   ╱│
             │    │    │    │    │    │   ...
             ●────●────●────●────●────●──
            ╱│   ╱│   ╱│   ╱│   ╱│   ╱│
             │    │    │    │    │    │
             ●────●────●────●────●────●──
            ╱    ╱    ╱    ╱    ╱    ╱

    Parameters
    ----------
    arrays : sequence of sequence of array
        The core tensor data arrays.
    shape : str, optional
        Which order the dimensions of the arrays are stored in, the default
        ``'urdlp'`` stands for ('up', 'right', 'down', 'left', 'physical').
        Arrays on the edge of lattice are assumed to be missing the
        corresponding dimension.
    tags : set[str], optional
        Extra global tags to add to the tensor network.
    site_ind_id : str, optional
        String specifier for naming convention of site indices.
    site_tag_id : str, optional
        String specifier for naming convention of site tags.
    row_tag_id : str, optional
        String specifier for naming convention of row tags.
    col_tag_id : str, optional
        String specifier for naming convention of column tags.
    """

    _EXTRA_PROPS = (
        '_site_tag_id',
        '_row_tag_id',
        '_col_tag_id',
        '_Lx',
        '_Ly',
        '_site_ind_id',
    )

    def __init__(self, arrays, *, shape='urdlp', tags=None,
                 site_ind_id='k{},{}', site_tag_id='I{},{}',
                 row_tag_id='ROW{}', col_tag_id='COL{}', **tn_opts):

        if isinstance(arrays, PEPS):
            super().__init__(arrays)
            return

        tags = tags_to_oset(tags)
        self._site_ind_id = site_ind_id
        self._site_tag_id = site_tag_id
        self._row_tag_id = row_tag_id
        self._col_tag_id = col_tag_id

        arrays = tuple(tuple(x for x in xs) for xs in arrays)
        self._Lx = len(arrays)
        self._Ly = len(arrays[0])
        tensors = []

        # cache for both creating and retrieving indices
        ix = defaultdict(rand_uuid)

        for i, j in product(range(self.Lx), range(self.Ly)):
            array = arrays[i][j]

            # figure out if we need to transpose the arrays from some order
            #     other than up right down left physical
            array_order = shape
            if i == self.Lx - 1:
                array_order = array_order.replace('u', '')
            if j == self.Ly - 1:
                array_order = array_order.replace('r', '')
            if i == 0:
                array_order = array_order.replace('d', '')
            if j == 0:
                array_order = array_order.replace('l', '')

            # allow convention of missing bonds to be singlet dimensions
            if len(array.shape) != len(array_order):
                array = do('squeeze', array)

            transpose_order = tuple(
                array_order.find(x) for x in 'urdlp' if x in array_order
            )
            if transpose_order != tuple(range(len(array_order))):
                array = do('transpose', array, transpose_order)

            # get the relevant indices corresponding to neighbours
            inds = []
            if 'u' in array_order:
                inds.append(ix[(i + 1, j), (i, j)])
            if 'r' in array_order:
                inds.append(ix[(i, j), (i, j + 1)])
            if 'd' in array_order:
                inds.append(ix[(i, j), (i - 1, j)])
            if 'l' in array_order:
                inds.append(ix[(i, j - 1), (i, j)])
            inds.append(self.site_ind(i, j))

            # mix site, row, column and global tags

            ij_tags = tags | oset((self.site_tag(i, j),
                                   self.row_tag(i),
                                   self.col_tag(j)))

            # create the site tensor!
            tensors.append(Tensor(data=array, inds=inds, tags=ij_tags))

        super().__init__(tensors, check_collisions=False, **tn_opts)

    @classmethod
    def rand(cls, Lx, Ly, bond_dim, phys_dim=2,
             dtype=float, seed=None, **peps_opts):
        """Create a random (un-normalized) PEPS.

        Parameters
        ----------
        Lx : int
            The number of rows.
        Ly : int
            The number of columns.
        bond_dim : int
            The bond dimension.
        physical : int, optional
            The physical index dimension.
        dtype : dtype, optional
            The dtype to create the arrays with, default is real double.
        seed : int, optional
            A random seed.
        peps_opts
            Supplied to :class:`~quimb.tensor.tensor_2d.PEPS`.

        Returns
        -------
        psi : PEPS
        """
        if seed is not None:
            seed_rand(seed)

        arrays = [[None for _ in range(Ly)] for _ in range(Lx)]

        for i, j in product(range(Lx), range(Ly)):

            shape = []
            if i != Lx - 1:  # bond up
                shape.append(bond_dim)
            if j != Ly - 1:  # bond right
                shape.append(bond_dim)
            if i != 0:  # bond down
                shape.append(bond_dim)
            if j != 0:  # bond left
                shape.append(bond_dim)
            shape.append(phys_dim)

            arrays[i][j] = ops.sensibly_scale(ops.sensibly_scale(
                randn(shape, dtype=dtype)))

        return cls(arrays, **peps_opts)

    def show(self):
        """Print a unicode schematic of this PEPS and its bond dimensions.
        """
        show_2d(self, show_lower=True)


class PEPO(TensorNetwork2DOperator,
           TensorNetwork2DFlat,
           TensorNetwork2D,
           TensorNetwork):
    r"""Projected Entangled Pair Operator object::


                         ...
             │╱   │╱   │╱   │╱   │╱   │╱
             ●────●────●────●────●────●──
            ╱│   ╱│   ╱│   ╱│   ╱│   ╱│
             │╱   │╱   │╱   │╱   │╱   │╱
             ●────●────●────●────●────●──
            ╱│   ╱│   ╱│   ╱│   ╱│   ╱│
             │╱   │╱   │╱   │╱   │╱   │╱   ...
             ●────●────●────●────●────●──
            ╱│   ╱│   ╱│   ╱│   ╱│   ╱│
             │╱   │╱   │╱   │╱   │╱   │╱
             ●────●────●────●────●────●──
            ╱    ╱    ╱    ╱    ╱    ╱

    Parameters
    ----------
    arrays : sequence of sequence of array
        The core tensor data arrays.
    shape : str, optional
        Which order the dimensions of the arrays are stored in, the default
        ``'urdlbk'`` stands for ('up', 'right', 'down', 'left', 'bra', 'ket').
        Arrays on the edge of lattice are assumed to be missing the
        corresponding dimension.
    tags : set[str], optional
        Extra global tags to add to the tensor network.
    upper_ind_id : str, optional
        String specifier for naming convention of upper site indices.
    lower_ind_id : str, optional
        String specifier for naming convention of lower site indices.
    site_tag_id : str, optional
        String specifier for naming convention of site tags.
    row_tag_id : str, optional
        String specifier for naming convention of row tags.
    col_tag_id : str, optional
        String specifier for naming convention of column tags.
    """

    _EXTRA_PROPS = (
        '_site_tag_id',
        '_row_tag_id',
        '_col_tag_id',
        '_Lx',
        '_Ly',
        '_upper_ind_id',
        '_lower_ind_id',
    )

    def __init__(self, arrays, *, shape='urdlbk', tags=None,
                 upper_ind_id='k{},{}', lower_ind_id='b{},{}',
                 site_tag_id='I{},{}', row_tag_id='ROW{}', col_tag_id='COL{}',
                 **tn_opts):

        if isinstance(arrays, PEPO):
            super().__init__(arrays)
            return

        tags = tags_to_oset(tags)
        self._upper_ind_id = upper_ind_id
        self._lower_ind_id = lower_ind_id
        self._site_tag_id = site_tag_id
        self._row_tag_id = row_tag_id
        self._col_tag_id = col_tag_id

        arrays = tuple(tuple(x for x in xs) for xs in arrays)
        self._Lx = len(arrays)
        self._Ly = len(arrays[0])
        tensors = []

        # cache for both creating and retrieving indices
        ix = defaultdict(rand_uuid)

        for i, j in product(range(self.Lx), range(self.Ly)):
            array = arrays[i][j]

            # figure out if we need to transpose the arrays from some order
            #     other than up right down left physical
            array_order = shape
            if i == self.Lx - 1:
                array_order = array_order.replace('u', '')
            if j == self.Ly - 1:
                array_order = array_order.replace('r', '')
            if i == 0:
                array_order = array_order.replace('d', '')
            if j == 0:
                array_order = array_order.replace('l', '')

            # allow convention of missing bonds to be singlet dimensions
            if len(array.shape) != len(array_order):
                array = do('squeeze', array)

            transpose_order = tuple(
                array_order.find(x) for x in 'urdlbk' if x in array_order
            )
            if transpose_order != tuple(range(len(array_order))):
                array = do('transpose', array, transpose_order)

            # get the relevant indices corresponding to neighbours
            inds = []
            if 'u' in array_order:
                inds.append(ix[(i + 1, j), (i, j)])
            if 'r' in array_order:
                inds.append(ix[(i, j), (i, j + 1)])
            if 'd' in array_order:
                inds.append(ix[(i, j), (i - 1, j)])
            if 'l' in array_order:
                inds.append(ix[(i, j - 1), (i, j)])
            inds.append(self.lower_ind(i, j))
            inds.append(self.upper_ind(i, j))

            # mix site, row, column and global tags
            ij_tags = tags | oset((self.site_tag(i, j),
                                   self.row_tag(i),
                                   self.col_tag(j)))

            # create the site tensor!
            tensors.append(Tensor(data=array, inds=inds, tags=ij_tags))

        super().__init__(tensors, check_collisions=False, **tn_opts)

    @classmethod
    def rand(cls, Lx, Ly, bond_dim, phys_dim=2, herm=False,
             dtype=float, seed=None, **pepo_opts):
        """Create a random PEPO.

        Parameters
        ----------
        Lx : int
            The number of rows.
        Ly : int
            The number of columns.
        bond_dim : int
            The bond dimension.
        physical : int, optional
            The physical index dimension.
        herm : bool, optional
            Whether to symmetrize the tensors across the physical bonds to make
            the overall operator hermitian.
        dtype : dtype, optional
            The dtype to create the arrays with, default is real double.
        seed : int, optional
            A random seed.
        pepo_opts
            Supplied to :class:`~quimb.tensor.tensor_2d.PEPO`.

        Returns
        -------
        X : PEPO
        """
        if seed is not None:
            seed_rand(seed)

        arrays = [[None for _ in range(Ly)] for _ in range(Lx)]

        for i, j in product(range(Lx), range(Ly)):

            shape = []
            if i != Lx - 1:  # bond up
                shape.append(bond_dim)
            if j != Ly - 1:  # bond right
                shape.append(bond_dim)
            if i != 0:  # bond down
                shape.append(bond_dim)
            if j != 0:  # bond left
                shape.append(bond_dim)
            shape.append(phys_dim)
            shape.append(phys_dim)

            X = ops.sensibly_scale(ops.sensibly_scale(
                randn(shape, dtype=dtype)))

            if herm:
                new_order = list(range(len(shape)))
                new_order[-2], new_order[-1] = new_order[-1], new_order[-2]
                X = (do('conj', X) + do('transpose', X, new_order)) / 2

            arrays[i][j] = X

        return cls(arrays, **pepo_opts)

    rand_herm = functools.partialmethod(rand, herm=True)

    def show(self):
        """Print a unicode schematic of this PEPO and its bond dimensions.
        """
        show_2d(self, show_lower=True, show_upper=True)


def show_2d(tn_2d, show_lower=False, show_upper=False):
    """Base function for printing a unicode schematic of flat 2D TNs.
    """

    lb = '╱' if show_lower else ' '
    ub = '╱' if show_upper else ' '

    line0 = ' ' + (f' {ub}{{:^3}}' * (tn_2d.Ly - 1)) + f' {ub}'
    bszs = [tn_2d.bond_size((0, j), (0, j + 1)) for j in range(tn_2d.Ly - 1)]

    lines = [line0.format(*bszs)]

    for i in range(tn_2d.Lx - 1):
        lines.append(' ●' + ('━━━━●' * (tn_2d.Ly - 1)))

        # vertical bonds
        lines.append(f'{lb}┃{{:<3}}' * tn_2d.Ly)
        bszs = [tn_2d.bond_size((i, j), (i + 1, j)) for j in range(tn_2d.Ly)]
        lines[-1] = lines[-1].format(*bszs)

        # horizontal bonds below
        lines.append(' ┃' + (f'{ub}{{:^3}}┃' * (tn_2d.Ly - 1)) + f'{ub}')
        bszs = [tn_2d.bond_size((i + 1, j), (i + 1, j + 1))
                for j in range(tn_2d.Ly - 1)]
        lines[-1] = lines[-1].format(*bszs)

    lines.append(' ●' + ('━━━━●' * (tn_2d.Ly - 1)))
    lines.append(f'{lb}    ' * tn_2d.Ly)

    print_multi_line(*lines)


def calc_plaquette_sizes(pairs, autogroup=True):
    """Find a sequence of plaquette blocksizes that will cover all the terms
    (coordinate pairs) in ``pairs``.

    Parameters
    ----------
    pairs : sequence of tuple[tuple[int]]
        The sequence of 2D coordinates pairs describing terms.
    autogroup : bool, optional
        Whether to return the minimal sequence of blocksizes that will cover
        all terms or merge them into a single ``((x_bsz, y_bsz),)``.

    Return
    ------
    bszs : tuple[tuple[int]]
        Pairs of blocksizes.

    Examples
    --------

    Some nearest neighbour interactions:

        >>> H2 = {None: qu.ham_heis(2)}
        >>> ham = qtn.LocalHam2D(10, 10, H2)
        >>> calc_plaquette_sizes(ham.terms.keys())
        ((1, 2), (2, 1))

        >>> calc_plaquette_sizes(ham.terms.keys(), autogroup=False)
        ((2, 2),)

    If we add any next nearest neighbour interaction then we are going to
    need the (2, 2) blocksize in any case:

        >>> H2[(1, 1), (2, 2)] = 0.5 * qu.ham_heis(2)
        >>> ham = qtn.LocalHam2D(10, 10, H2)
        >>> calc_plaquette_sizes(ham.terms.keys())
        ((2, 2),)

    If we add longer range interactions (non-diagonal next nearest) we again
    can benefit from multiple plaquette blocksizes:

        >>> H2[(1, 1), (1, 3)] = 0.25 * qu.ham_heis(2)
        >>> H2[(1, 1), (3, 1)] = 0.25 * qu.ham_heis(2)
        >>> ham = qtn.LocalHam2D(10, 10, H2)
        >>> calc_plaquette_sizes(ham.terms.keys())
        ((1, 3), (2, 2), (3, 1))

    Or choose the plaquette blocksize that covers all terms:

        >>> calc_plaquette_sizes(ham.terms.keys(), autogroup=False)
        ((3, 3),)

    """
    # get the rectangular size of each coordinate pair
    #     e.g. ((1, 1), (2, 1)) -> (2, 1)
    #          ((4, 5), (6, 7)) -> (3, 3) etc.
    bszs = {tuple(abs(a - b) + 1 for a, b in zip(*pair)) for pair in pairs}

    # remove block size pairs that can be contained in another block pair size
    #     e.g. {(1, 2), (2, 1), (2, 2)} -> ((2, 2),)
    bszs = tuple(sorted(
        b for b in bszs
        if not any(
            (b[0] <= b2[0]) and (b[1] <= b2[1])
            for b2 in bszs - {b}
        )
    ))

    # return each plaquette size separately
    if autogroup:
        return bszs

    # else choose a single blocksize that will cover all terms
    #     e.g. ((1, 2), (3, 2)) -> ((3, 2),)
    #          ((1, 2), (2, 1)) -> ((2, 2),)
    return (tuple(map(max, zip(*bszs))),)


def plaquette_to_sites(p):
    """Turn a plaquette ``((i0, j0), (di, dj))`` into the sites it contains.

    Examples
    --------

        >>> plaquette_to_sites([(3, 4), (2, 2)])
        ((3, 4), (3, 5), (4, 4), (4, 5))
    """
    (i0, j0), (di, dj) = p
    return tuple((i, j)
                 for i in range(i0, i0 + di)
                 for j in range(j0, j0 + dj))


def calc_plaquette_map(plaquettes):
    """Generate a dictionary of all the coordinate pairs in ``plaquettes``
    mapped to the 'best' (smallest) rectangular plaquette that contains them.

    Examples
    --------

    Consider 4 sites, with one 2x2 plaquette and two vertical (2x1)
    and horizontal (1x2) plaquettes each:

        >>> plaquettes = [
        ...     # 2x2 plaquette covering all sites
        ...     ((0, 0), (2, 2)),
        ...     # horizontal plaquettes
        ...     ((0, 0), (1, 2)),
        ...     ((1, 0), (1, 2)),
        ...     # vertical plaquettes
        ...     ((0, 0), (2, 1)),
        ...     ((0, 1), (2, 1)),
        ... ]

        >>> calc_plaquette_map(plaquettes)
        {((0, 0), (0, 1)): ((0, 0), (1, 2)),
         ((0, 0), (1, 0)): ((0, 0), (2, 1)),
         ((0, 0), (1, 1)): ((0, 0), (2, 2)),
         ((0, 1), (1, 0)): ((0, 0), (2, 2)),
         ((0, 1), (1, 1)): ((0, 1), (2, 1)),
         ((1, 0), (1, 1)): ((1, 0), (1, 2))}

    Now every of the size coordinate pairs is mapped to one of the plaquettes,
    but to the smallest one that contains it. So the 2x2 plaquette (specified
    by ``((0, 0), (2, 2))``) would only used for diagonal terms here.
    """
    # sort in descending total plaquette size
    plqs = sorted(plaquettes, key=lambda p: (-p[1][0] * p[1][1], p))

    mapping = dict()
    for p in plqs:
        sites = plaquette_to_sites(p)
        # this will generate all coordinate pairs with ij_a < ij_b
        for ij_a, ij_b in combinations(sites, 2):
            mapping[ij_a, ij_b] = p

    return mapping


def gen_long_range_path(ij_a, ij_b, sequence=None):
    """Generate a string of coordinates, in order, from ``ij_a`` to ``ij_b``.

    Parameters
    ----------
    ij_a : (int, int)
        Coordinate of site 'a'.
    ij_b : (int, int)
        Coordinate of site 'b'.
    sequence : None, iterable of {'v', 'h'}, or 'random', optional
        What order to cycle through and try and perform moves in, 'v', 'h'
        standing for move vertically and horizontally respectively. The default
        is ``('v', 'h')``.

    Returns
    -------
    generator[tuple[int]]
        The path, each element is a single coordinate.
    """
    ia, ja = ij_a
    ib, jb = ij_b
    di = ib - ia
    dj = jb - ja

    # nearest neighbour
    if abs(di) + abs(dj) == 1:
        yield ij_a
        yield ij_b
        return

    if sequence is None:
        poss_moves = cycle(('v', 'h'))
    elif sequence == 'random':
        poss_moves = (random.choice('vh') for _ in count())
    else:
        poss_moves = cycle(sequence)

    yield ij_a

    for move in poss_moves:
        if abs(di) + abs(dj) == 1:
            yield ij_b
            return

        if (move == 'v') and (di != 0):
            # move a vertically
            istep = min(max(di, -1), +1)
            new_ij_a = (ia + istep, ja)
            yield new_ij_a
            ij_a = new_ij_a
            ia += istep
            di -= istep
        elif (move == 'h') and (dj != 0):
            # move a horizontally
            jstep = min(max(dj, -1), +1)
            new_ij_a = (ia, ja + jstep)
            yield new_ij_a
            ij_a = new_ij_a
            ja += jstep
            dj -= jstep


def gen_long_range_swap_path(ij_a, ij_b, sequence=None):
    """Generate the coordinates or a series of swaps that would bring ``ij_a``
    and ``ij_b`` together.

    Parameters
    ----------
    ij_a : (int, int)
        Coordinate of site 'a'.
    ij_b : (int, int)
        Coordinate of site 'b'.
    sequence : None, it of {'av', 'bv', 'ah', 'bh'}, or 'random', optional
        What order to cycle through and try and perform moves in, 'av', 'bv',
        'ah', 'bh' standing for move 'a' vertically, 'b' vertically, 'a'
        horizontally', and 'b' horizontally respectively. The default is
        ``('av', 'bv', 'ah', 'bh')``.

    Returns
    -------
    generator[tuple[tuple[int]]]
        The path, each element is two coordinates to swap.
    """
    ia, ja = ij_a
    ib, jb = ij_b
    di = ib - ia
    dj = jb - ja

    # nearest neighbour
    if abs(di) + abs(dj) == 1:
        yield (ij_a, ij_b)
        return

    if sequence is None:
        poss_moves = cycle(('av', 'bv', 'ah', 'bh'))
    elif sequence == 'random':
        poss_moves = (random.choice(('av', 'bv', 'ah', 'bh')) for _ in count())
    else:
        poss_moves = cycle(sequence)

    for move in poss_moves:
        if (move == 'av') and (di != 0):
            # move a vertically
            istep = min(max(di, -1), +1)
            new_ij_a = (ia + istep, ja)
            yield (ij_a, new_ij_a)
            ij_a = new_ij_a
            ia += istep
            di -= istep

        elif (move == 'bv') and (di != 0):
            # move b vertically
            istep = min(max(di, -1), +1)
            new_ij_b = (ib - istep, jb)
            # need to make sure final gate is applied correct way
            if new_ij_b == ij_a:
                yield (ij_a, ij_b)
            else:
                yield (ij_b, new_ij_b)
            ij_b = new_ij_b
            ib -= istep
            di -= istep

        elif (move == 'ah') and (dj != 0):
            # move a horizontally
            jstep = min(max(dj, -1), +1)
            new_ij_a = (ia, ja + jstep)
            yield (ij_a, new_ij_a)
            ij_a = new_ij_a
            ja += jstep
            dj -= jstep

        elif (move == 'bh') and (dj != 0):
            # move b horizontally
            jstep = min(max(dj, -1), +1)
            new_ij_b = (ib, jb - jstep)
            # need to make sure final gate is applied correct way
            if new_ij_b == ij_a:
                yield (ij_a, ij_b)
            else:
                yield (ij_b, new_ij_b)
            ij_b = new_ij_b
            jb -= jstep
            dj -= jstep

        if di == dj == 0:
            return


def swap_path_to_long_range_path(swap_path, ij_a):
    """Generates the ordered long-range path - a sequence of coordinates - from
    a (long-range) swap path - a sequence of coordinate pairs.
    """
    sites = set(chain(*swap_path))
    return sorted(sites, key=lambda ij_b: manhattan_distance(ij_a, ij_b))


@functools.lru_cache(8)
def get_swap(dp, dtype, backend):
    SWAP = swap(dp, dtype=dtype)
    return do('array', SWAP, like=backend)
