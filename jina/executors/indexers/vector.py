__copyright__ = "Copyright (c) 2020 Jina AI Limited. All rights reserved."
__license__ = "Apache-2.0"

import gzip
import os
from functools import lru_cache
from os import path
from typing import Optional, List, Union, Tuple, Dict

import numpy as np

from . import BaseVectorIndexer
from ..decorators import batching
from ...helper import cached_property


class BaseNumpyIndexer(BaseVectorIndexer):
    """:class:`BaseNumpyIndexer` stores and loads vector in a compresses binary file

    .. note::
        :attr:`compress_level` balances between time and space. By default, :classL`NumpyIndexer` has
        :attr:`compress_level` = 0.

        Setting :attr:`compress_level`>0 gives a smaller file size on the disk in the index time. However, in the query
        time it loads all data into memory at once. Not ideal for large scale application.

        Setting :attr:`compress_level`=0 enables :func:`np.memmap`, which loads data in an on-demanding way and
        gives smaller memory footprint in the query time. However, it often gives larger file size on the disk.


    """

    def __init__(self,
                 compress_level: int = 1,
                 ref_indexer: 'BaseNumpyIndexer' = None,
                 *args, **kwargs):
        """
        :param compress_level: The compresslevel argument is an integer from 0 to 9 controlling the
                        level of compression; 1 is fastest and produces the least compression,
                        and 9 is slowest and produces the most compression. 0 is no compression
                        at all. The default is 9.
        :param ref_indexer: Bootstrap the current indexer from a ``ref_indexer``. This enables user to switch
                            the query algorithm at the query time.

        """
        super().__init__(*args, **kwargs)
        self.num_dim = None
        self.dtype = None
        self.compress_level = compress_level
        self.key_bytes = b''
        self.key_dtype = None
        self._ref_index_abspath = None

        if ref_indexer:
            # copy the header info of the binary file
            self.num_dim = ref_indexer.num_dim
            self.dtype = ref_indexer.dtype
            self.compress_level = ref_indexer.compress_level
            self.key_bytes = ref_indexer.key_bytes
            self.key_dtype = ref_indexer.key_dtype
            self._size = ref_indexer._size
            # point to the ref_indexer.index_filename
            # so that later in `post_init()` it will load from the referred index_filename
            self._ref_index_abspath = ref_indexer.index_abspath

    @property
    def index_abspath(self) -> str:
        """Get the file path of the index storage

        Use index_abspath

        """
        return getattr(self, '_ref_index_abspath', None) or self.get_file_from_workspace(self.index_filename)

    def get_add_handler(self):
        """Open a binary gzip file for adding new vectors

        :return: a gzip file stream
        """
        if self.compress_level > 0:
            return gzip.open(self.index_abspath, 'ab', compresslevel=self.compress_level)
        else:
            return open(self.index_abspath, 'ab')

    def get_create_handler(self):
        """Create a new gzip file for adding new vectors

        :return: a gzip file stream
        """
        if self.compress_level > 0:
            return gzip.open(self.index_abspath, 'wb', compresslevel=self.compress_level)
        else:
            return open(self.index_abspath, 'wb')

    def _validate_key_vector_shapes(self, keys, vectors):
        if len(vectors.shape) != 2:
            raise ValueError(f'vectors shape {vectors.shape} is not valid, expecting "vectors" to have rank of 2')

        if not getattr(self, 'num_dim', None):
            self.num_dim = vectors.shape[1]
            self.dtype = vectors.dtype.name
        elif self.num_dim != vectors.shape[1]:
            raise ValueError(
                f'vectors shape {vectors.shape} does not match with indexers\'s dim: {self.num_dim}')
        elif self.dtype != vectors.dtype.name:
            raise TypeError(
                f'vectors\' dtype {vectors.dtype.name} does not match with indexers\'s dtype: {self.dtype}')
        elif keys.shape[0] != vectors.shape[0]:
            raise ValueError(f'number of key {keys.shape[0]} not equal to number of vectors {vectors.shape[0]}')
        elif self.key_dtype != keys.dtype.name:
            raise TypeError(
                f'keys\' dtype {keys.dtype.name} does not match with indexers keys\'s dtype: {self.key_dtype}')

    def add(self, keys: 'np.ndarray', vectors: 'np.ndarray', *args, **kwargs) -> None:
        self._validate_key_vector_shapes(keys, vectors)
        self.write_handler.write(vectors.tobytes())
        self.key_bytes += keys.tobytes()
        self.key_dtype = keys.dtype.name
        self._size += keys.shape[0]

    def get_query_handler(self) -> Optional['np.ndarray']:
        """Open a gzip file and load it as a numpy ndarray

        :return: a numpy ndarray of vectors
        """
        vecs = self.raw_ndarray
        if vecs is not None:
            return self.build_advanced_index(vecs)

    def build_advanced_index(self, vecs: 'np.ndarray'):
        """
        Build advanced index structure based on in-memory numpy ndarray, e.g. graph, tree, etc.

        :param vecs: the raw numpy ndarray
        :return:
        """
        raise NotImplementedError

    def _load_gzip(self, abspath: str) -> Optional['np.ndarray']:
        try:
            self.logger.info(f'loading index from {abspath}...')
            with gzip.open(abspath, 'rb') as fp:
                return np.frombuffer(fp.read(), dtype=self.dtype).reshape([-1, self.num_dim])
        except EOFError:
            self.logger.error(
                f'{abspath} is broken/incomplete, perhaps forgot to ".close()" in the last usage?')

    @cached_property
    def raw_ndarray(self) -> Optional['np.ndarray']:
        if not (path.exists(self.index_abspath) or self.num_dim or self.dtype):
            return

        if self.compress_level > 0:
            return self._load_gzip(self.index_abspath)
        elif self.size is not None and os.stat(self.index_abspath).st_size:
            self.logger.success(f'memmap is enabled for {self.index_abspath}')
            return np.memmap(self.index_abspath, dtype=self.dtype, mode='r', shape=(self.size, self.num_dim))

    def query_by_id(self, ids: Union[List[int], 'np.ndarray'], *args, **kwargs) -> 'np.ndarray':
        int_ids = [self.ext2int_id[j] for j in ids]
        return self.raw_ndarray[int_ids]

    @cached_property
    def int2ext_id(self) -> Optional['np.ndarray']:
        """Convert internal ids (0,1,2,3,4,...) to external ids (random index) """
        if self.key_bytes and self.key_dtype:
            r = np.frombuffer(self.key_bytes, dtype=self.key_dtype)
            if r.shape[0] == self.size == self.raw_ndarray.shape[0]:
                return r
            else:
                self.logger.error(
                    f'the size of the keys and vectors are inconsistent '
                    f'({r.shape[0]}, {self._size}, {self.raw_ndarray.shape[0]}), '
                    f'did you write to this index twice? or did you forget to save indexer?')

    @cached_property
    def ext2int_id(self) -> Optional[Dict]:
        """Convert external ids (random index) to internal ids (0,1,2,3,4,...) """
        if self.int2ext_id is not None:
            return {k: idx for idx, k in enumerate(self.int2ext_id)}


@lru_cache(maxsize=3)
def _get_ones(x, y):
    return np.ones((x, y))


def _ext_A(A):
    nA, dim = A.shape
    A_ext = _get_ones(nA, dim * 3)
    A_ext[:, dim:2 * dim] = A
    A_ext[:, 2 * dim:] = A ** 2
    return A_ext


def _ext_B(B):
    nB, dim = B.shape
    B_ext = _get_ones(dim * 3, nB)
    B_ext[:dim] = (B ** 2).T
    B_ext[dim:2 * dim] = -2.0 * B.T
    del B
    return B_ext


def _euclidean(A_ext, B_ext):
    sqdist = A_ext.dot(B_ext).clip(min=0)
    return np.sqrt(sqdist)


def _norm(A):
    return A / np.linalg.norm(A, ord=2, axis=1, keepdims=True)


def _cosine(A_norm_ext, B_norm_ext):
    return A_norm_ext.dot(B_norm_ext).clip(min=0) / 2


class NumpyIndexer(BaseNumpyIndexer):
    """An exhaustive vector indexers implemented with numpy and scipy. """

    batch_size = 512

    def __init__(self, metric: str = 'euclidean',
                 backend: str = 'numpy',
                 compress_level: int = 0,
                 *args, **kwargs):
        """
        :param metric: The distance metric to use. `braycurtis`, `canberra`, `chebyshev`, `cityblock`, `correlation`,
                        `cosine`, `dice`, `euclidean`, `hamming`, `jaccard`, `jensenshannon`, `kulsinski`,
                        `mahalanobis`,
                        `matching`, `minkowski`, `rogerstanimoto`, `russellrao`, `seuclidean`, `sokalmichener`,
                        `sokalsneath`, `sqeuclidean`, `wminkowski`, `yule`.
        :param backend: `numpy` or `scipy`, `numpy` only supports `euclidean` and `cosine` distance

        .. note::
            Metrics other than `cosine` and `euclidean` requires ``scipy`` installed.

        """
        super().__init__(*args, compress_level=compress_level, **kwargs)
        self.metric = metric
        self.backend = backend

    def query(self, keys: 'np.ndarray', top_k: int, *args, **kwargs) -> Tuple['np.ndarray', 'np.ndarray']:
        """ Find the top-k vectors with smallest ``metric`` and return their ids.

        :return: a tuple of two ndarray.
            The first is ids in shape B x K (`dtype=int`), the second is metric in shape B x K (`dtype=float`)

        .. warning::
            This operation is memory-consuming.

            Distance (the smaller the better) is returned, not the score.

        """
        if self.metric not in {'cosine', 'euclidean'} or self.backend == 'scipy':
            dist = self._cdist(keys, self.query_handler)
        elif self.metric == 'euclidean':
            _keys = _ext_A(keys)
            dist = self._euclidean(_keys, self.query_handler)
        elif self.metric == 'cosine':
            _keys = _ext_A(_norm(keys))
            dist = self._cosine(_keys, self.query_handler)
        else:
            raise NotImplementedError(f'{self.metric} is not implemented')

        idx = dist.argsort(axis=1)[:, :top_k]
        dist = np.take_along_axis(dist, idx, axis=1)
        return self.int2ext_id[idx], dist

    def build_advanced_index(self, vecs: 'np.ndarray'):
        return vecs

    @batching(merge_over_axis=1, slice_on=2)
    def _euclidean(self, cached_A, raw_B):
        data = _ext_B(raw_B)
        return _euclidean(cached_A, data)

    @batching(merge_over_axis=1, slice_on=2)
    def _cosine(self, cached_A, raw_B):
        data = _ext_B(_norm(raw_B))
        return _cosine(cached_A, data)

    @batching(merge_over_axis=1, slice_on=2)
    def _cdist(self, *args, **kwargs):
        try:
            from scipy.spatial.distance import cdist
            return cdist(*args, **kwargs, metric=self.metric)
        except ModuleNotFoundError:
            raise ModuleNotFoundError(f'your metric {self.metric} requires scipy, but scipy is not found')
