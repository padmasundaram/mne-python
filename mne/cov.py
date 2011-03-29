# Authors: Alexandre Gramfort <gramfort@nmr.mgh.harvard.edu>
#          Matti Hamalainen <msh@nmr.mgh.harvard.edu>
#
# License: BSD (3-clause)

import os
import numpy as np
from scipy import linalg

from .fiff.constants import FIFF
from .fiff.tag import find_tag
from .fiff.tree import dir_tree_find
from .fiff.proj import read_proj
from .fiff.channels import _read_bad_channels

from .fiff.write import start_block, end_block, write_int, write_name_list, \
                       write_double, write_float_matrix, start_file, end_file
from .fiff.proj import write_proj, make_projector
from .fiff import fiff_open
from .fiff.pick import pick_types


def rank(A, tol=1e-8):
    s = linalg.svd(A, compute_uv=0)
    return np.sum(np.where(s > s[0]*tol, 1, 0))


def _get_whitener(A, rnk, pca, ch_type):
    # whitening operator
    D, V = linalg.eigh(A, overwrite_a=True)
    I = np.argsort(D)[::-1]
    D = D[I]
    V = V[:, I]
    D = 1.0 / D
    if not pca: # No PCA case.
        print 'Not doing PCA for %s.' % ch_type
        W = np.sqrt(D)[:, None] * V.T
    else: # Rey's approach. MNE has been changed to implement this.
        print 'Setting small %s eigenvalues to zero.' % ch_type
        D[rnk:] = 0.0
        W = np.sqrt(D)[:, None] * V.T
        # This line will reduce the actual number of variables in data
        # and leadfield to the true rank.
        W = W[:rnk]
    return W


class Covariance(object):
    """Noise covariance matrix"""

    _kind_to_id = dict(full=1, sparse=2, diagonal=3) # XXX : check
    _id_to_kind = {1: 'full', 2: 'sparse', 3: 'diagonal'} # XXX : check

    def __init__(self, kind='full'):
        self.kind = kind

    def load(self, fname):
        """load covariance matrix from FIF file"""

        if self.kind in Covariance._kind_to_id:
            cov_kind = Covariance._kind_to_id[self.kind]
        else:
            raise ValueError('Unknown type of covariance. '
                             'Choose between full, sparse or diagonal.')

        # Reading
        fid, tree, _ = fiff_open(fname)
        cov = read_cov(fid, tree, cov_kind)
        fid.close()

        self._cov = cov
        self.data = cov['data']
        self.ch_names = cov['names']

    def save(self, fname):
        """save covariance matrix in a FIF file"""
        write_cov_file(fname, self._cov)

    def whitener(self, info, mag_reg=0.1, grad_reg=0.1, eeg_reg=0.1, pca=True):
        """Compute whitener based on a list of channels

        Parameters
        ----------
        info : dict
            Measurement info of data to apply the whitener.
            Defines data channels and which are the bad channels
            to be ignored.
        mag_reg : float
            Regularization of the magnetometers.
            Recommended between 0.05 and 0.2
        grad_reg : float
            Regularization of the gradiometers.
            Recommended between 0.05 and 0.2
        eeg_reg : float
            Regularization of the EGG channels.
            Recommended between 0.05 and 0.2
        pca : bool
            If True, whitening is restricted to the space of
            the data. It makes sense when data have a low rank
            due to SSP or maxfilter.

        Returns
        -------
        W : array
            Whitening matrix
        ch_names : list of strings
            List of channel names on which to apply the whitener.
            It corresponds to the columns of W.
        """

        if pca and self.kind == 'diagonal':
            print "Setting pca to False with a diagonal covariance matrix."
            pca = False

        bads = info['bads']
        C_idx = [k for k, name in enumerate(self.ch_names)
                 if name in info['ch_names'] and name not in bads]
        ch_names = [self.ch_names[k] for k in C_idx]
        C_noise = self.data[np.ix_(C_idx, C_idx)] # take covariance submatrix

        # Create the projection operator
        proj, ncomp, _ = make_projector(info['projs'], ch_names)
        if ncomp > 0:
            print '\tCreated an SSP operator (subspace dimension = %d)' % ncomp
            C_noise = np.dot(proj, np.dot(C_noise, proj.T))

        # Regularize Noise Covariance Matrix.
        variances = np.diag(C_noise)
        ind_meg = pick_types(info, meg=True, eeg=False, exclude=bads)
        names_meg = [info['ch_names'][k] for k in ind_meg]
        C_ind_meg = [ch_names.index(name) for name in names_meg]

        ind_grad = pick_types(info, meg='grad', eeg=False, exclude=bads)
        names_grad = [info['ch_names'][k] for k in ind_grad]
        C_ind_grad = [ch_names.index(name) for name in names_grad]

        ind_mag = pick_types(info, meg='mag', eeg=False, exclude=bads)
        names_mag = [info['ch_names'][k] for k in ind_mag]
        C_ind_mag = [ch_names.index(name) for name in names_mag]

        ind_eeg = pick_types(info, meg=False, eeg=True, exclude=bads)
        names_eeg = [info['ch_names'][k] for k in ind_eeg]
        C_ind_eeg = [ch_names.index(name) for name in names_eeg]

        has_meg = len(ind_meg) > 0
        has_eeg = len(ind_eeg) > 0

        if self.kind == 'diagonal':
            C_noise = np.diag(variances)
            rnkC_noise = len(variances)
            print 'Rank of noise covariance is %d' % rnkC_noise
        else:
            # estimate noise covariance matrix rank
            # Loop on all the required data types (MEG MAG, MEG GRAD, EEG)

            if has_meg: # Separate rank of MEG
                rank_meg = rank(C_noise[C_ind_meg][:, C_ind_meg])
                print 'Rank of MEG part of noise covariance is %d' % rank_meg
            if has_eeg: # Separate rank of EEG
                rank_eeg = rank(C_noise[C_ind_eeg][:, C_ind_eeg])
                print 'Rank of EEG part of noise covariance is %d' % rank_eeg

            for ind, reg in zip([C_ind_grad, C_ind_mag, C_ind_eeg],
                                [grad_reg, mag_reg, eeg_reg]):
                if len(ind) > 0:
                    # add constant on diagonal
                    C_noise[ind, ind] += reg * np.mean(variances[ind])

            if has_meg and has_eeg: # Sets cross terms to zero
                C_noise[np.ix_(C_ind_meg, C_ind_eeg)] = 0.0
                C_noise[np.ix_(C_ind_eeg, C_ind_meg)] = 0.0

        # whitening operator
        if has_meg:
            W_meg = _get_whitener(C_noise[C_ind_meg][:, C_ind_meg], rank_meg,
                                  pca, 'MEG')

        if has_eeg:
            W_eeg = _get_whitener(C_noise[C_ind_eeg][:, C_ind_eeg], rank_eeg,
                                  pca, 'EEG')

        if has_meg and not has_eeg: # Only MEG case.
            W = W_meg
        elif has_eeg and not has_meg: # Only EEG case.
            W = W_eeg
        elif has_eeg and has_meg: # Bimodal MEG and EEG case.
            # Whitening of MEG and EEG separately, which assumes zero
            # covariance between MEG and EEG (i.e., a block diagonal noise
            # covariance). This was recommended by Matti as EEG does not
            # measure all the signals from the same environmental noise sources
            # as MEG.
            W = np.r_[np.c_[W_meg, np.zeros((W_meg.shape[0], W_eeg.shape[1]))],
                      np.c_[np.zeros((W_eeg.shape[0], W_meg.shape[1])), W_eeg]]

        return W, names_meg + names_eeg

    def estimate_from_raw(self, raw, picks=None, quantum_sec=10):
        """Estimate noise covariance matrix from a raw FIF file
        """
        #   Set up the reading parameters
        start = raw.first_samp
        stop = raw.last_samp + 1
        quantum = int(quantum_sec * raw.info['sfreq'])

        cov = 0
        n_samples = 0

        # Read data
        for first in range(start, stop, quantum):
            last = first + quantum
            if last >= stop:
                last = stop

            data, times = raw[picks, first:last]

            if self.kind is 'full':
                cov += np.dot(data, data.T)
            elif self.kind is 'diagonal':
                cov += np.diag(np.sum(data ** 2, axis=1))
            else:
                raise ValueError("Unsupported covariance kind")

            n_samples += data.shape[1]

        self.data = cov / n_samples # XXX : check
        print '[done]'

    def __repr__(self):
        s = "kind : %s" % self.kind
        s += ", size : %s x %s" % self.data.shape
        s += ", data : %s" % self.data
        return "Covariance (%s)" % s


def read_cov(fid, node, cov_kind):
    """Read a noise covariance matrix

    Parameters
    ----------
    fid: file
        The file descriptor

    node: dict
        The node in the FIF tree

    cov_kind: int
        The type of covariance. XXX : clarify

    Returns
    -------
    data: dict
        The noise covariance
    """
    #   Find all covariance matrices
    covs = dir_tree_find(node, FIFF.FIFFB_MNE_COV)
    if len(covs) == 0:
        raise ValueError('No covariance matrices found')

    #   Is any of the covariance matrices a noise covariance
    for p in range(len(covs)):
        tag = find_tag(fid, covs[p], FIFF.FIFF_MNE_COV_KIND)
        if tag is not None and tag.data == cov_kind:
            this = covs[p]

            #   Find all the necessary data
            tag = find_tag(fid, this, FIFF.FIFF_MNE_COV_DIM)
            if tag is None:
                raise ValueError('Covariance matrix dimension not found')

            dim = tag.data
            tag = find_tag(fid, this, FIFF.FIFF_MNE_COV_NFREE)
            if tag is None:
                nfree = -1
            else:
                nfree = tag.data

            tag = find_tag(fid, this, FIFF.FIFF_MNE_ROW_NAMES)
            if tag is None:
                names = []
            else:
                names = tag.data.split(':')
                if len(names) != dim:
                    raise ValueError('Number of names does not match '
                                       'covariance matrix dimension')

            tag = find_tag(fid, this, FIFF.FIFF_MNE_COV)
            if tag is None:
                tag = find_tag(fid, this, FIFF.FIFF_MNE_COV_DIAG)
                if tag is None:
                    raise ValueError('No covariance matrix data found')
                else:
                    #   Diagonal is stored
                    data = tag.data
                    diagmat = True
                    print '\t%d x %d diagonal covariance (kind = %d) found.' \
                                                        % (dim, dim, cov_kind)

            else:
                from scipy import sparse
                if not sparse.issparse(tag.data):
                    #   Lower diagonal is stored
                    vals = tag.data
                    data = np.zeros((dim, dim))
                    data[np.tril(np.ones((dim, dim))) > 0] = vals
                    data = data + data.T
                    data.flat[::dim+1] /= 2.0
                    diagmat = False
                    print '\t%d x %d full covariance (kind = %d) found.' \
                                                        % (dim, dim, cov_kind)
                else:
                    diagmat = False
                    data = tag.data
                    print '\t%d x %d sparse covariance (kind = %d) found.' \
                                                        % (dim, dim, cov_kind)

            #   Read the possibly precomputed decomposition
            tag1 = find_tag(fid, this, FIFF.FIFF_MNE_COV_EIGENVALUES)
            tag2 = find_tag(fid, this, FIFF.FIFF_MNE_COV_EIGENVECTORS)
            if tag1 is not None and tag2 is not None:
                eig = tag1.data
                eigvec = tag2.data
            else:
                eig = None
                eigvec = None

            #   Read the projection operator
            projs = read_proj(fid, this)

            #   Read the bad channel list
            bads = _read_bad_channels(fid, this)

            #   Put it together
            cov = dict(kind=cov_kind, diag=diagmat, dim=dim, names=names,
                       data=data, projs=projs, bads=bads, nfree=nfree, eig=eig,
                       eigvec=eigvec)
            return cov

    raise ValueError('Did not find the desired covariance matrix')

    return None

###############################################################################
# Writing

def write_cov(fid, cov):
    """Write a noise covariance matrix

    Parameters
    ----------
    fid: file
        The file descriptor

    cov: dict
        The noise covariance matrix to write
    """
    start_block(fid, FIFF.FIFFB_MNE_COV)

    #   Dimensions etc.
    write_int(fid, FIFF.FIFF_MNE_COV_KIND, cov['kind'])
    write_int(fid, FIFF.FIFF_MNE_COV_DIM, cov['dim'])
    if cov['nfree'] > 0:
        write_int(fid, FIFF.FIFF_MNE_COV_NFREE, cov['nfree'])

    #   Channel names
    if cov['names'] is not None:
        write_name_list(fid, FIFF.FIFF_MNE_ROW_NAMES, cov['names'])

    #   Data
    if cov['diag']:
        write_double(fid, FIFF.FIFF_MNE_COV_DIAG, cov['data'])
    else:
        # Store only lower part of covariance matrix
        dim = cov['dim']
        mask = np.tril(np.ones((dim, dim), dtype=np.bool)) > 0
        vals = cov['data'][mask].ravel()
        write_double(fid, FIFF.FIFF_MNE_COV, vals)

    #   Eigenvalues and vectors if present
    if cov['eig'] is not None and cov['eigvec'] is not None:
        write_float_matrix(fid, FIFF.FIFF_MNE_COV_EIGENVECTORS, cov['eigvec'])
        write_double(fid, FIFF.FIFF_MNE_COV_EIGENVALUES, cov['eig'])

    #   Projection operator
    write_proj(fid, cov['projs'])

    #   Bad channels
    if cov['bads'] is not None:
        start_block(fid, FIFF.FIFFB_MNE_BAD_CHANNELS)
        write_name_list(fid, FIFF.FIFF_MNE_CH_NAME_LIST, cov['bads'])
        end_block(fid, FIFF.FIFFB_MNE_BAD_CHANNELS)

    #   Done!
    end_block(fid, FIFF.FIFFB_MNE_COV)


def write_cov_file(fname, cov):
    """Write a noise covariance matrix

    Parameters
    ----------
    fname: string
        The name of the file

    cov: dict
        The noise covariance
    """
    fid = start_file(fname)

    try:
        write_cov(fid, cov)
    except Exception as inst:
        os.remove(fname)
        raise '%s', inst

    end_file(fid)
