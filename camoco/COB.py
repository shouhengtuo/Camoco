#!/usr/bin/python3

import warnings

warnings.simplefilter(action="ignore", category=FutureWarning)

import camoco.PCCUP as PCCUP


from .Camoco import Camoco
from .RefGen import RefGen
from .Locus import Locus, Gene
from .Expr import Expr
from .Tools import memoize, available_datasets
from .Term import Term
from .Ontology import Ontology

from math import isinf
from numpy import matrix, arcsinh, tanh
from collections import defaultdict, Counter
from itertools import chain
from matplotlib.collections import LineCollection
from subprocess import Popen, PIPE
from scipy.spatial.distance import squareform
from scipy.special import comb
from scipy.stats import norm, pearsonr
from scipy.cluster.hierarchy import linkage, leaves_list, dendrogram
from statsmodels.sandbox.regression.predstd import wls_prediction_std
from io import UnsupportedOperation

import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import statsmodels.api as sm
import networkx as nx
import pandas as pd
import numpy as np
import itertools
import fastcluster
import psutil

from odo import odo

from matplotlib import rcParams

rcParams.update({"figure.autolayout": True})

import operator
import statsmodels.api as sm
import sys
import pdb
import json
import gc


class COB(Expr):
    """
        A COB object represents an easily browsable Co-expression network.
        (COB-> co-expression browser)
    """

    def __init__(self, name):  # pragma: no cover
        """
        Initialize a built co-expression network

        Parameters
        ----------
        name : str
            The name of the co-expression network (from when it was built)

        Returns
        -------
        cob : COB
            A COB object
        """
        super().__init__(name=name)
        self.log("Loading Coex table")
        self.coex = self._bcolz("coex", blaze=True)
        self.sigs = None
        if self.coex is None:
            self.log("{} is empty", name)
        if not self._global("significance_threshold") is None:
            self.set_sig_edge_zscore(float(self._global("significance_threshold")))
        self.log("Loading Global Degree")
        self.degree = self._bcolz("degree")
        if self.degree is None:
            self.log("{} is empty", name)
        if (
            not available_datasets("Ontology", "{}MCL".format(name))
            and self.coex is not None
        ):
            self._calculate_clusters()
        self.log("Loading Clusters")
        self.clusters = self._bcolz("clusters")
        if self.clusters is None:
            self.log("Clusters not loaded for: {} ()", name)
            self.MCL = None
        else:
            self.MCL = Ontology("{}MCL".format(self.name))

    def __repr__(self):
        return "<COB: {}>".format(self.name)

    def __str__(self):
        return self.__repr__()

    def summary(self, file=sys.stdout):  # pragma: no cover
        """
        Returns a nice summary of what is in the network.

        Parameters
        ----------
        file : str,default=stdout,optional

        Returns
        -------
        None
            The summary is printed either to stdout or a provided file.
        """
        import camoco as co
        print(
           f"""
            CAMOCO (version:{co.__version__})
            ---------------------------------------

            COB Dataset: {self.name}
                Desc: {self.description}
                RawType: {self.rawtype}
                TransformationLog: {self._transformation_log}
                Num Genes: {self.num_genes():,}({(self.num_genes() / self.num_genes(raw=True)) * 100:.2g}% of total)
                Num Accessions: {self.num_accessions()}

            Network Stats
            -------------
            Unthresholded Interactions: {len(self.coex):,}
            Thresholded (Z >= {self._global("current_significance_threshold")}): {len(self.sigs):,}

            Raw
            ------------------
            Num Raw Genes: {len(self.expr(raw=True)):,}
            Num Raw Accessions: {len(self.expr(raw=True).columns)}

            QC Parameters
            ------------------
            min expr level: {self._global("qc_min_expr")} 
                - expression below this is set to NaN
            max gene missing data: {self._global("qc_max_gene_missing_data")} 
                - genes missing more than this percent are removed
            max accession missing data: {self._global("qc_max_accession_missing_data")}
                - Accession missing more than this percent are removed
            min single sample expr: {self._global("qc_min_single_sample_expr")} 
                - genes must have this amount of expression in 
                  at least one accession.

            Clusters
            ------------------
            Num clusters (size >= 10): {sum(self.clusters.groupby("cluster").apply(len) >= 10)}
        """,
            file=file,
        )

    def qc_gene(self):
        """
            Returns qc statistics broken down by chromosome

            Paramaters
            ----------
            None

            Returns
            -------
            DataFrame
                A dataframe containing QC info
        """
        qc_gene = self._bcolz("qc_gene")
        # generate the parent refegen
        rg = self._parent_refgen
        qc_gene["chrom"] = [rg[x].chrom if x in rg else "None" for x in qc_gene.index]
        return qc_gene.groupby("chrom").aggregate(sum, axis=0)

    @property
    @memoize
    def edge_FDR(self):  # C
        """
        Returns a calculated false discovery rate of the Edges. This is 
        calculated from the number of expected edges from the standard normal
        distribution, which a network will follow if the gene expression matrix
        is simply random data. This function looks at the number of expected 
        'significant' edges and divides that by the number of observed edges in 
        the network.

        Parameters
        -----------
        None

        Returns
        -------
        FDR : float
            The ratio of expected edges / observed edges
        """
        # get the percent of significant edges
        num_sig = self.coex.significant.coerce(to="int32").sum() / len(self.coex)
        # calulate the number expected
        num_exp = 1 - norm.cdf(float(self._global("significance_threshold")))
        # FDR is the percentage expected over the percentage found
        return num_exp / num_sig

    def set_sig_edge_zscore(self, zscore):
        """
        Sets the 'significance' threshold for the coex network. This will
        affect thresholded network metrics that use degree (e.g. locality)
        It will not affect unthresholded metrics like Density. 

        Parameters
        ----------
        zscore : float
            the new significance threshold

        Returns
        -------
        None
        """
        # Don't do anything if there isn't a coex table
        if self.coex is None:
            return
        # Only update if needed
        cur_sig = self._global("current_significance_threshold")
        new_sig = cur_sig is None or not (float(cur_sig) == zscore)

        # Set the new significant value
        if new_sig:
            # If the column doesn't exist because of an error it may fail
            try:
                self.coex.data.delcol(name="significant")
            except ValueError:
                pass
            # Add the column to the underlying data structure
            self.coex.data.addcol(
                self.coex.data.eval("score >= " + str(zscore)),
                pos=2,
                name="significant",
            )
            self.coex.data.flush()

            # Keep track of the current threshold
            self._global("current_significance_threshold", zscore)
            self._calculate_degree(update_db=False)

        # Rebuild significant index set
        if new_sig or self.sigs is None:
            self.sigs = np.array(
                [ind for ind in self.coex.data["significant"].wheretrue()]
            )
            self.sigs.sort()
        return None

    def _coex_DataFrame(self, ids=None, sig_only=True):
        """
            Converts the underlying coexpression table into
            a pandas dataframe 

            Parameters
            ----------
                ids : array-like of ints (default: None)
                    Indices to include in the data frame. Usually
                    computed from another COB method (e.g. 
                    PCCUP.coex_index). If None, then all indices
                    will be included.
                sig_only : bool (default: True)
                    If true, only "significant" edges will be 
                    included in the table. If False, all edges will
                    be included.

            Returns
            -------
                A Pandas Dataframe


            .. warning:: This will put the entire gene-by-accession
                         dataframe into memory.
        """
        # If no ids are provided, get all of them
        if ids is None:
            if sig_only:
                ids = self.sigs
            else:
                return self.coex.data.todataframe()
        else:
            ids.sort()
            if sig_only:
                ids = np.intersect1d(ids, self.sigs, assume_unique=True)

        # Get the DataFrame
        df = pd.DataFrame.from_items(
            ((key, self.coex.data[key][ids]) for key in self.coex.data.names)
        )
        # df = odo(self.coex[ids],pd.DataFrame)
        df.set_index(ids, inplace=True)
        return df

    def neighbors(
        self,
        gene,
        sig_only=True,
        names_as_index=True,
        names_as_cols=False,
        return_gene_set=False,
    ):
        """
            Returns a DataFrame containing the neighbors for gene.

            Parameters
            ----------
            gene : co.Locus
                The gene for which to extract neighbors
            sig_only : bool (default: True)
                A flag to include only significant interactions.
            names_as_index : bool (default: True)
                Include gene names as the index. If this and `names_as_cols` are
                both False, only the interactions are returned which is a faster
                operation than including gene names.
            names_as_cols : bool (default: False)
                Include gene names as two columns named 'gene_a' and 'gene_b'.
            return_gene_set : bool (default: False)
                Return the set of neighbors instead of a dataframe

            Returns
            -------
            - A DataFrame containing edges 
            - A Gene set IF return_gene_set is true

        """
        # Find the neighbors
        gene_id = self._get_gene_index(gene)
        ids = PCCUP.coex_neighbors(gene_id, self.num_genes())
        edges = self._coex_DataFrame(ids=ids, sig_only=sig_only)
        del ids
        if len(edges) == 0:
            edges = pd.DataFrame(
                columns=["gene_a", "gene_b", "score", "distance", "significant"]
            )
            if names_as_cols:
                return edges
            else:
                return edges.set_index(["gene_a", "gene_b"])
        if return_gene_set:
            names_as_index = True
        # Find the indexes if necessary
        if names_as_index or names_as_cols:
            names = self._expr.index.values
            ids = edges.index.values
            ids = PCCUP.coex_expr_index(ids, self.num_genes())
            edges.insert(0, "gene_a", names[ids[:, 0]])
            edges.insert(1, "gene_b", names[ids[:, 1]])
            del ids
            del names
        if return_gene_set:
            neighbors = set(self.refgen[set(edges["gene_a"]).union(edges["gene_b"])])
            if len(neighbors) == 1:
                return set()
            neighbors.remove(gene)
            return neighbors
        if names_as_index and not names_as_cols:
            edges = edges.set_index(["gene_a", "gene_b"])

        return edges

    def neighborhood(self, gene_list, return_genes=False, neighbors_only=False):
        """ 
            Find the genes that have network connections the the gene_list.
        
            Parameters
            ----------
            Input: A gene List
                The gene list used to obtain the neighborhood.
            
            Returns
            -------
            A Dataframe containing gene ids which have at least
            one edge with another gene in the input list. Also returns
            global degree
        """
        if isinstance(gene_list, Locus):
            gene_list = [gene_list]
        gene_list = set(gene_list)
        neighbors = set()
        for gene in gene_list:
            neighbors.update(self.neighbors(gene, sig_only=True, return_gene_set=True))
        # Remove the neighbors who are in the gene_list
        neighbors = neighbors.difference(gene_list)
        if return_genes == False:
            neighbors = pd.DataFrame({"gene": [x.id for x in neighbors]})
            neighbors["neighbor"] = True
            local = pd.DataFrame({"gene": [x.id for x in gene_list]})
            local["neighbor"] = False
            if neighbors_only == False:
                return pd.concat([local, neighbors])
            else:
                return neighbors
        elif return_genes == True:
            if neighbors_only == False:
                neighbors.update(gene_list)
                return neighbors
            else:
                return neighbors

    def next_neighbors(
        self, gene_list, n=None, return_table=False, include_query=False
    ):
        """ 
            Given a set of input genes, return the next (n) neighbors
            that have the stronges connection to the input set.

            Parameters
            ----------
            gene_list : list-like of co.Locus
                An iterable of genes for which the next neighbors will be 
                calculated.
            n : int (default: None)
                The number of next neighbors to return. If None, the method
                will return ALL neighbors
            return_table : bool (default:False)
                If true, a table with neighbors and scores will be 
                returned
            include_query : bool (default:False)
                If True (and return table is False) the query gene(s) will
                be included in the return list

            Returns
            -------
            returns a list containing the strongest connected neighbors 
        """
        if isinstance(gene_list, Locus):
            gene_list = [gene_list]
        neighbors = defaultdict(lambda: 0)
        for gene in set(gene_list):
            edges = self.neighbors(gene, names_as_cols=True)
            source_id = gene.id
            for g1, g2, score in zip(edges["gene_a"], edges["gene_b"], edges["score"]):
                if g1 == source_id:
                    neighbors[g2] += score
                else:
                    neighbors[g1] += score

        neighbors = sorted(neighbors.items(), key=operator.itemgetter(1), reverse=True)
        if n != None:
            neighbors = neighbors[:n]
        if return_table == True:
            return pd.DataFrame(neighbors, columns=["neighbor", "score"])
        else:
            neighbors = set(self.refgen[[x[0] for x in neighbors]])
            if include_query == True:
                neighbors.update(gene_list)
            return neighbors

    def coexpression(self, gene_a, gene_b):
        """
            Returns a coexpression z-score between two genes. This
            is the pearson correlation coefficient of the two genes'
            expression profiles across the accessions (experiments).
            This value is pulled from the

            Parameters
            ----------
            gene_a : camoco.Locus
                The first gene
            gene_b : camoco.Locus
                The second gene

            Returns
            Coexpression Z-Score

        """
        if gene_a.id == gene_b.id:
            # We don't cache these results
            score = self._coex_concordance(gene_a, gene_b)
            significant = 0
            distance = 0
            return pd.Series(
                [score, significant, distance],
                name=(gene_a.id, gene_b.id),
                index=["score", "significant", "distance"],
            )
        return self.subnetwork([gene_a, gene_b], sig_only=False).iloc[0]

    def subnetwork(
        self,
        gene_list=None,
        sig_only=True,
        min_distance=None,
        filter_missing_gene_ids=True,
        trans_locus_only=False,
        names_as_index=True,
        names_as_cols=False,
    ):
        """
            Extract a subnetwork of edges exclusively between genes
            within the gene_list. Also includes various options for
            what information to report, see Parameters.

            Parameters
            ----------
            gene_list : iter of Loci
                The genes from which to extract a subnetwork.
                If gene_list is None, the function will assume
                gene_list is all genes in COB object (self).
            sig_only : bool
                A flag to include only significant interactions.
            min_distance : bool (default: None)
                If not None, only include interactions that are
                between genes that are a `min_distance` away from
                one another.
            filter_missing_gene_ids : bool (default: True)
                Filter out gene ids that are not in the current
                COB object (self).
            trans_locus_only : bool (default: True)
                Filter out gene interactions that are not in Trans,
                this argument requires that locus attr object has
                the 'parent_locus' key:val set to distinguish between
                cis and trans elements.
            names_as_index : bool (default: True)
                Include gene names as the index.
            names_as_cols : bool (default: False)
                Include gene names as two columns named 'gene_a' and 'gene_b'.

            Returns
            -------
            A pandas.DataFrame containing the edges. Columns
            include score, significant (bool), and inter-genic distance.
        """
        num_genes = self.num_genes()
        if gene_list is None:
            # Return the entire DataFrame
            df = self._coex_DataFrame(sig_only=sig_only)
        else:
            # Extract the ids for each Gene
            gene_list = set(sorted(gene_list))
            ids = np.array([self._expr_index[x.id] for x in gene_list])
            if filter_missing_gene_ids:
                # filter out the Nones
                ids = np.array([x for x in ids if x is not None])
            if len(ids) == 0:
                df = pd.DataFrame(columns=["score", "significant", "distance"])
            else:
                # Grab the coexpression indices for the genes
                ids = PCCUP.coex_index(ids, num_genes)
                df = self._coex_DataFrame(ids=ids, sig_only=sig_only)
                del ids
        if min_distance is not None:
            df = df[df.distance >= min_distance]
        if names_as_index or names_as_cols or trans_locus_only:
            names = self._expr.index.values
            ids = df.index.values
            if len(ids) > 0:
                ids = PCCUP.coex_expr_index(ids, num_genes)
                df.insert(0, "gene_a", names[ids[:, 0]])
                df.insert(1, "gene_b", names[ids[:, 1]])
                del ids
                del names
            else:
                df.insert(0, "gene_a", [])
                df.insert(0, "gene_b", [])
        if names_as_index and not names_as_cols:
            df = df.set_index(["gene_a", "gene_b"])
        if trans_locus_only:
            try:
                parents = {x.id: x.attr["parent_locus"] for x in gene_list}
            except KeyError as e:
                raise KeyError(
                    "Each locus must have 'parent_locus'"
                    " attr set to calculate trans only"
                )
            df["trans"] = [
                parents[gene_a] != parents[gene_b]
                for gene_a, gene_b in zip(
                    df.index.get_level_values(0), df.index.get_level_values(1)
                )
            ]
        return df

    def trans_locus_density(
        self,
        locus_list,
        flank_limit,
        return_mean=True,
        bootstrap=False,
        by_gene=False,
        iter_name=None,
    ):
        """
            Calculates the density of edges which span loci. Must take in a locus
            list so we can exlude cis-locus interactions.

            Parameters
            ----------
            locus_list : iter of Loci
                an iterable of loci
            flank_limit : int
                The number of flanking genes passed to be pulled out
                for each locus (passed onto the refgen.candidate_genes method)
            return_mean : bool (default: True)
                If false, raw edges will be returned
            bootstrap : bool (default: False)
                If true, candidate genes will be bootstrapped from the COB
                reference genome
            by_gene : bool (default: False)
                Return a per-gene breakdown of density within the subnetwork.
            iter_name : str (default: None)
                Optional string which will be added as a column. Useful for
                keeping track of bootstraps in an aggregated data frame.

            Returns
            -------
            Z-score of interactions if return_mean is True
            otherwise a dataframe of trans edges

        """
        # convert to list of loci to lists of genes
        if not bootstrap:
            genes_list = self.refgen.candidate_genes(
                locus_list,
                flank_limit=flank_limit,
                chain=True,
                include_parent_locus=True,
            )
        else:
            genes_list = self.refgen.bootstrap_candidate_genes(
                locus_list,
                flank_limit=flank_limit,
                chain=True,
                include_parent_locus=True,
            )
        # Extract the edges for the full set of genes
        edges = self.subnetwork(
            genes_list,
            min_distance=0,
            sig_only=False,
            trans_locus_only=True,
            names_as_index=True,
        )
        if by_gene == True:
            # Filter out trans edges
            gene_split = pd.DataFrame.from_records(
                chain(
                    *[
                        ((gene_a, score), (gene_b, score))
                        for gene_a, gene_b, score, *junk in edges[edges.trans == True]
                        .reset_index()
                        .values
                    ]
                ),
                columns=["gene", "score"],
            )
            gene_split = gene_split.groupby("gene").agg(np.mean)
            if iter_name is not None:
                gene_split["iter"] = iter_name
            gene_split.index.name = "gene"
            gene_split["num_trans_edges"] = len(edges)
            return gene_split
        else:
            if return_mean:
                scores = edges.loc[edges["trans"] == True, "score"]
                return np.nanmean(scores) / (1 / np.sqrt(len(scores)))
            else:
                return edges.loc[edges["trans"] == True,]

    def trans_locus_locality(
        self,
        locus_list,
        flank_limit,
        bootstrap=False,
        by_gene=False,
        iter_name=None,
        include_regression=False,
    ):
        """
            Computes a table comparing local degree to global degree
            of genes COMPUTED from a set of loci.
            NOTE: interactions from genes originating from the same
            locus are not counted for global or local degree.

            Parameters
            ----------
            locus_list : iterable of camoco.Loci
                A list or equivalent of loci
            flank_limit : int
                The number of flanking genes passed to be pulled out
                for each locus (passed onto the refgen.candidate_genes method)
            bootstrap : bool (default: False)
                If true, candidate genes will be bootstrapped from the COB
                reference genome
            iter_name : object (default: none)
                This will be added as a column. Useful for
                generating bootstraps of locality and keeping
                track of which one a row came from after catting
                multiple bootstraps together.
            by_gene : bool (default: False)
                Return a per-gene breakdown of density within the subnetwork.
            include_regression : bool (default: False)
                Include the OLS regression residuals and fitted values
                on local ~ global.

            Returns
            -------
            A pandas DataFrame with local, global and residual columns
            based on linear regression of local on global degree.
        """
        # convert to list of loci to lists of genes
        if not bootstrap:
            genes_list = self.refgen.candidate_genes(
                locus_list,
                flank_limit=flank_limit,
                chain=True,
                include_parent_locus=True,
            )
        else:
            genes_list = self.refgen.bootstrap_candidate_genes(
                locus_list,
                flank_limit=flank_limit,
                chain=True,
                include_parent_locus=True,
            )
        # self.log("Found {} candidate genes", len(genes_list))
        # Get global and local degree for candidates
        gdegree = self.global_degree(genes_list, trans_locus_only=True)
        ldegree = self.local_degree(genes_list, trans_locus_only=True)
        # Merge the columns
        degree = ldegree.merge(gdegree, left_index=True, right_index=True)
        degree.columns = ["local", "global"]
        degree = degree.sort_values(by="global")
        degree.index.name = "gene"
        if include_regression:
            # Add the regression lines
            loc_deg = degree["local"]
            glob_deg = degree["global"]
            ols = sm.OLS(loc_deg.astype(float), glob_deg.astype(float)).fit()
            degree["resid"] = ols.resid
            degree["fitted"] = ols.fittedvalues
            degree = degree.sort_values(by="resid", ascending=False)
        if iter_name is not None:
            degree["iter"] = iter_name
        return degree

    def density(self, gene_list, min_distance=None, by_gene=False):
        """
            Calculates the density of the non-thresholded network edges
            amongst genes within gene_list. Includes parameters to perform
            measurements for genes within a certain distance of each other.
            This corrects for cis regulatory elements increasing noise
            in coexpression network.

            Parameters
            ----------
            gene_list : iter of Loci
                List of genes from which to calculate density.
            min_distance : int (default: None)
                Ignore edges between genes less than min_distance
                in density calculation.
            by_gene : bool (default: False)
                Return a per-gene breakdown of density within the subnetwork.

            Returns
            -------
            A network density OR density on a gene-wise basis
        """
        # filter for only genes within network
        edges = self.subnetwork(gene_list, min_distance=min_distance, sig_only=False)

        if by_gene == True:
            x = pd.DataFrame.from_records(
                chain(
                    *[
                        ((gene_a, score), (gene_b, score))
                        for gene_a, gene_b, score, sig, dis in edges.reset_index().values
                    ]
                ),
                columns=["gene", "score"],
            )
            return x.groupby("gene").agg(np.mean)
        else:
            if len(edges) == 0:
                return np.nan
            if len(edges) == 1:
                return edges.score[0]
            return np.nanmean(edges.score) / (1 / np.sqrt(len(edges)))

    def to_dat(self, gene_list=None, filename=None, sig_only=True, min_distance=0):
        """
            Outputs a .DAT file (see Sleipnir library)
        """
        if filename is None:
            filename = self.name + ".dat"
        with open(filename, "w") as OUT:
            # Get the score table
            self.log("Pulling the scores for the .dat")
            score = self.subnetwork(
                gene_list,
                sig_only=sig_only,
                min_distance=min_distance,
                names_as_index=False,
                names_as_cols=False,
            )

            # Drop unecessary columns
            score.drop(["distance", "significant"], axis=1, inplace=True)

            # Find the ids from those
            self.log("Finding the IDs")
            names = self._expr.index.values
            ids = PCCUP.coex_expr_index(score.index.values, self.num_genes())
            score.insert(0, "gene_a", names[ids[:, 0]])
            score.insert(1, "gene_b", names[ids[:, 1]])
            del ids
            del names

            # Print it out!
            self.log("Writing the .dat")
            score.to_csv(
                OUT, columns=["gene_a", "gene_b", "score"], index=False, sep="\t"
            )
            del score
            self.log("Done")

    def to_graphml(self, file, gene_list=None, sig_only=True, min_distance=0):
        """
        """
        # Get the edge indexes
        self.log("Getting the network.")
        edges = self.subnetwork(
            gene_list=gene_list,
            sig_only=sig_only,
            min_distance=min_distance,
            names_as_index=False,
            names_as_cols=False,
        ).index.values

        # Find the ids from those
        names = self._expr.index.values
        edges = PCCUP.coex_expr_index(edges, self.num_genes())
        df = pd.DataFrame(index=np.arange(edges.shape[0]))
        df["gene_a"] = names[edges[:, 0]]
        df["gene_b"] = names[edges[:, 1]]
        del edges
        del names

        # Build the NetworkX network
        self.log("Building the graph.")
        net = nx.from_pandas_dataframe(df, "gene_a", "gene_b")
        del df

        # Print the file
        self.log("Writing the file.")
        nx.write_graphml(net, file)
        del net
        return

    def to_json(
        self,
        gene_list=None,
        filename=None,
        sig_only=True,
        min_distance=None,
        max_edges=None,
        remove_orphans=True,
        ontology=None,
        include_coordinates=True,
        invert_y_coor=True,
        min_degree=None,
        include_edges=True
    ):
        """
            Produce a JSON network object that can be loaded in cytoscape.js
            or Cytoscape v3+.

            Parameters
            ----------
            gene_list : iterable of Locus objects
                These loci or more specifically, genes,
                must be in the COB RefGen object,
                they are the genes in the network.
            filename : str (default None)
                If specified, the JSON string will be output to 
                file. 
            sig_only : bool (default: True)
                Flag specifying whether or not to only
                include the significant edges only. If
                False, **All pairwise interactions** will
                be included. (warning: it can be large).
            min_distance : bool (default: None)
                If specified, only interactions between
                genes larger than this distance will be 
                included. This corrects for potential 
                cis-biased co-expression.
            max_edges : int (default: None)
                If specified, only the maximum number of 
                edges will be included. Priority of edges
                is assigned based on score.
            remove_orphans : bool (default: True)
                Remove genes that have no edges in the 
                networ#.
            ontology :#camoco.Ontology (default: None)
                If an ontology is specified, genes will
                be annotated to belonging to terms within 
                the ontology. This is useful for highlighting 
                groups of genes once they are inside of
                cytoscape(.js).
            include_coordinates : bool (default: True)
                If true, include coordinates for available
                genes. Genes without calculated coordinates will
                be left blank.
            invert_y_coor : boor (default: True)
                If True, the y-coordinate will be inverted (y=-1*y).
                For some reason Cytoscape has an inverted y-coordinate
                system, toggling this will fix it.

            Returns
            -------
            A JSON string or None if a filename is specified
        """
        net = {"nodes": [], "edges": []}
        # calculate included genes
        if gene_list is None:
            gene_list = self.genes()
        # Filter by minimum degree
        if min_degree is not None:
            included = set(self.degree.query(f'Degree >= {min_degree}').index)
            gene_list = [x for x in gene_list if x.id in included]
        # Get the edge indexes
        self.log("Getting the network.")
        edges = self.subnetwork(
            gene_list=gene_list,
            sig_only=sig_only,
            min_distance=min_distance,
            names_as_index=False,
            names_as_cols=True,
        )
        if max_edges != None:
            # Filter out only the top X edges by score
            edges = edges.sort_values(by="score", ascending=False)[0:max_edges]

        if include_coordinates == True:
            # Create a map with x,y coordinates
            coor = self.coordinates() 
            if invert_y_coor:
                coor.y = -1*coor.y
            coor_map = { 
                id:coor for id,coor in zip(coor.index,zip(coor.x,coor.y))
            }
        # Add edges to json data structure
        if include_edges:
            for source, target, score, distance, significant in edges.itertuples(
                index=False
            ):
                net["edges"].append(
                    {
                        "data": {
                            "source": source,
                            "target": target,
                            "score": float(score),
                            "distance": float(fix_val(distance)),
                        }
                    }
                )
        # Handle any ontological business
        if ontology != None:
            # Make a map from gene name to ontology
            ont_map = defaultdict(set)
            for term in ontology.iter_terms():
                for locus in term.loci:
                    ont_map[locus.id].add(term.id)

        parents = defaultdict(list)
        # generate the subnetwork for the genes
        if gene_list == None:
            gene_list = list(self.refgen.iter_genes())
        else:
            gene_list = set(gene_list)
        if remove_orphans == True:
            # get a list of all the genes with edges
            has_edges = set(edges.gene_a).union(edges.gene_b)
            gene_list = [x for x in gene_list if x.id in has_edges]
        for gene in gene_list:
            node = {"data": {"id": str(gene.id), "classes": "gene"}}
            if ontology != None and gene.id in ont_map:
                for x in ont_map[gene.id]:
                    node["data"][x] = True
            node["data"].update(gene.attr)
            if include_coordinates:
                try:
                    pos = coor_map[gene.id]
                except KeyError:
                    pos = (0,0)
                node['position'] = {
                    "x" : pos[0],
                    "y" : pos[1]
                }

            net["nodes"].append(node)

        # Return the correct output
        net = {"elements": net}
        if filename:
            with open(filename, "w") as OUT:
                print(json.dumps(net), file=OUT)
                del net
        else:
            net = json.dumps(net)
            return net

    def to_sparse_matrix(
        self, gene_list=None, 
        min_distance=None, 
        max_edges=None, 
        remove_orphans=False
    ):
        """
            Convert the co-expression interactions to a 
            scipy sparse matrix.

            Parameters
            -----
            gene_list: iter of Loci (default: None)
                If specified, return only the interactions among
                loci in the list. If None, use all genes.
            min_distance : int (default: None)
                The minimum distance between genes for which to consider
                co-expression interactions. This filters out cis edges.
            max_edges : int (default: None)
                If specified, only the maximum number of 
                edges will be included. Priority of edges
                is assigned based on score.
            remove_orphans : bool (default: True)
                Remove genes that have no edges in the 
                network.


            Returns
            -------
            A tuple (a,b) where 'a' is a scipy sparse matrix and
            'b' is a mapping from gene_id to index.
        """
        from scipy import sparse

        self.log("Getting genes")
        # first get the subnetwork in pair form
        self.log("Pulling edges")
        edges = self.subnetwork(
            gene_list=gene_list,
            min_distance=min_distance,
            sig_only=True,
            names_as_cols=True,
            names_as_index=False,
        )
        # Option to limit the number of edges
        if max_edges is not None:
            self.log("Filtering edges")
            edges = edges.sort_values(by="score", ascending=False)[
                0 : min(max_edges, len(edges))
            ]
        # Create a gene index
        self.log("Creating Index")
        if gene_list == None:
            gene_list = list(self.refgen.iter_genes())
        else:
            gene_list = set(gene_list)
        gene_index = {g.id: i for i, g in enumerate(gene_list)}
        nlen = len(gene_list)
        # Option to restrict gene list to only genes with edges
        if remove_orphans:
            self.log("Removing orphans")
            not_orphans = set(edges.gene_a).union(edges.gene_b)
            gene_list = [g for g in gene_list if g.id in not_orphans]
            self.log(f"Removed {len()}")
        # get the expression matrix indices for all the genes
        row = [gene_index[x] for x in edges.gene_a.values]
        col = [gene_index[x] for x in edges.gene_b.values]
        data = list(edges.score.values)
        # Make the values symmetric by doubling everything
        # Note: by nature we dont have cycles so we dont have to
        #   worry about the diagonal
        self.log("Making matrix symmetric")
        d = data + data
        r = row + col
        c = col + row
        self.log("Creating matrix")
        matrix = sparse.coo_matrix((d, (r, c)), shape=(nlen, nlen), dtype=None)
        return (matrix, gene_index)

    def mcl(
        self,
        gene_list=None,
        I=2.0,
        min_distance=None,
        min_cluster_size=0,
        max_cluster_size=10e10,
    ):
        """
            Returns clusters (as list) as designated by MCL (Markov Clustering).

            Parameters
            ----------
            gene_list : a gene iterable
                These are the genes which will be clustered
            I : float (default: 2.0)
                This is the inflation parameter passed into mcl.
            min_distance : int (default: None)
                The minimum distance between genes for which to consider
                co-expression interactions. This filters out cis edges.
            min_cluster_size : int (default: 0)
                The minimum cluster size to return. Filter out clusters smaller
                than this.
            max_cluster_size : float (default: 10e10)
                The maximum cluster size to return. Filter out clusters larger
                than this.

            Returns
            -------
            A list clusters containing a lists of genes within each cluster
        """
        import markov_clustering as mc

        matrix, gene_index = self.to_sparse_matrix(gene_list=gene_list)
        # Run MCL
        result = mc.run_mcl(
            matrix,
            inflation=I,
            verbose=True
        )
        clusters = mc.get_clusters(result)
        # MCL traditionally returns clusters by size with 0 being the largest
        clusters = sorted(clusters, key=lambda x: len(x), reverse=True)
        # Create a dictionary to map ids to gene names
        gene_id_index = {v: k for k, v in gene_index.items()}
        result = []
        for c in clusters:
            if len(c) < min_cluster_size or len(c) > max_cluster_size:
                continue
            # convert to loci
            loci = self.refgen.from_ids([gene_id_index[i] for i in c])
            result.append(loci)
        return result

    def _mcl_legacy(
        self,
        gene_list=None,
        I=2.0,
        scheme=7,
        min_distance=None,
        min_cluster_size=0,
        max_cluster_size=10e10,
    ):
        """
            A *very* thin wrapper to the MCL program. The MCL program must
            be accessible by a subprocess (i.e. by the shell).
            Returns clusters (as list) as designated by MCL.

            Parameters
            ----------
            gene_list : a gene iterable
                These are the genes which will be clustered
            I : float (default: 2.0)
                This is the inflation parameter passed into mcl.
            scheme : int in 1:7
                MCL accepts parameter schemes. See mcl docs for more details
            min_distance : int (default: None)
                The minimum distance between genes for which to consider
                co-expression interactions. This filters out cis edges.
            min_cluster_size : int (default: 0)
                The minimum cluster size to return. Filter out clusters smaller
                than this.
            max_cluster_size : float (default: 10e10)
                The maximum cluster size to return. Filter out clusters larger
                than this.

            Returns
            -------
            A list clusters containing a lists of genes within each cluster
        """
        # output dat to tmpfile
        tmp = self._tmpfile()
        self.to_dat(
            filename=tmp.name,
            gene_list=gene_list,
            min_distance=min_distance,
            sig_only=True,
        )
        # build the mcl command
        cmd = "mcl {} --abc -scheme {} -I {} -o -".format(tmp.name, scheme, I)
        self.log("running MCL: {}", cmd)
        try:
            p = Popen(cmd, stdout=PIPE, stderr=sys.stderr, shell=True)
            self.log("waiting for MCL to finish...")
            sout = p.communicate()[0]
            p.wait()
            self.log("MCL done, Reading results.")
            if p.returncode == 0:
                # Filter out cluters who are smaller than the min size
                return list(
                    filter(
                        lambda x: len(x) > min_cluster_size
                        and len(x) < max_cluster_size,
                        # Generate ids from the refgen
                        [
                            self.refgen.from_ids(
                                [gene.decode("utf-8") for gene in line.split()]
                            )
                            for line in sout.splitlines()
                        ],
                    )
                )
            else:
                if p.returncode == 127:
                    raise FileNotFoundError()
                else:
                    raise ValueError("MCL failed: return code: {}".format(p.returncode))
        except FileNotFoundError as e:
            self.log(
                'Could not find MCL in PATH. Make sure its installed and shell accessible as "mcl".'
            )

    def local_degree(self, gene_list, trans_locus_only=False):
        """
            Returns the local degree of a list of genes

            Parameters
            ----------
            gene_list : iterable (co.Locus object)
                a list of genes for which to retrieve local degree for. The
                genes must be in the COB object (of course)
            trans_locus_only : bool (default: False)
                only count edges if they are from genes originating from
                different loci. Each gene MUST have 'parent_locus' set in
                its attr object.
        """
        subnetwork = self.subnetwork(
            gene_list, sig_only=True, trans_locus_only=trans_locus_only
        )
        if trans_locus_only:
            subnetwork = subnetwork.ix[subnetwork.trans]
        local_degree = pd.DataFrame(
            list(Counter(chain(*subnetwork.index.get_values())).items()),
            columns=["Gene", "Degree"],
        ).set_index("Gene")
        # We need to find genes not in the subnetwork and add them as degree 0
        # The code below is ~optimized~
        # DO NOT alter unless you know what you're doing :)
        degree_zero_genes = pd.DataFrame(
            [(gene.id, 0) for gene in gene_list if gene.id not in local_degree.index],
            columns=["Gene", "Degree"],
        ).set_index("Gene")
        return pd.concat([local_degree, degree_zero_genes])

    def global_degree(self, gene_list, trans_locus_only=False):
        """
            Returns the global degree of a list of genes
    
            Parameters
            ----------
            gene_list : iterable (co.Locus object)
                a list of genes for which to retrieve local degree for. The
                genes must be in the COB object (of course)
            trans_locus_only : bool (default: False)
                only count edges if they are from genes originating from
                different loci. Each gene MUST have 'parent_locus' set in
                its attr object.
        """
        try:
            if isinstance(gene_list, Locus):
                if trans_locus_only:
                    raise ValueError("Cannot calculate cis degree on one gene.")
                return self.degree.loc[gene_list.id].Degree
            else:
                degree = self.degree.ix[[x.id for x in gene_list]].fillna(0)
                if trans_locus_only:
                    degree = degree - self.cis_degree(gene_list)
                return degree
        except KeyError as e:
            return 0

    def cis_degree(self, gene_list):
        """
            Returns the number of *cis* interactions for each gene in the gene
            list. Two genes are is *cis* if they share the same parent locus.
            **Therefore: each gene object MUST have its 'parent_locus' attr set!!**

            Parameters
            ----------
            gene_list : iterable of Gene Objects
        """
        subnetwork = self.subnetwork(gene_list, sig_only=True, trans_locus_only=True)
        # Invert the trans column
        subnetwork["cis"] = np.logical_not(subnetwork.trans)
        subnetwork = subnetwork.ix[subnetwork.cis]
        local_degree = pd.DataFrame(
            list(Counter(chain(*subnetwork.index.get_values())).items()),
            columns=["Gene", "Degree"],
        ).set_index("Gene")
        # We need to find genes not in the subnetwork and add them as degree 0
        # The code below is ~optimized~
        # DO NOT alter unless you know what you're doing :)
        degree_zero_genes = pd.DataFrame(
            [(gene.id, 0) for gene in gene_list if gene.id not in local_degree.index],
            columns=["Gene", "Degree"],
        ).set_index("Gene")
        return pd.concat([local_degree, degree_zero_genes])

    def locality(self, gene_list, iter_name=None, include_regression=False):
        """
            Computes the merged local vs global degree table

            Parameters
            ----------
            gene_list : iterable of camoco.Loci
                A list or equivalent of loci
            iter_name : object (default: none)
                This will be added as a column. Useful for
                generating bootstraps of locality and keeping
                track of which one a row came from after catting
                multiple bootstraps together.
            include_regression : bool (default: False)
                Include the OLS regression residuals and fitted values
                on local ~ global.

            Returns
            -------
            A pandas DataFrame with local, global and residual columns
            based on linear regression of local on global degree.

        """
        global_degree = self.global_degree(gene_list)
        local_degree = self.local_degree(gene_list)
        degree = global_degree.merge(local_degree, left_index=True, right_index=True)
        degree.columns = ["global", "local"]
        degree = degree.sort_values(by="global")
        if include_regression:
            # set up variables to use astype to aviod pandas sm.OLS error
            loc_deg = degree["local"]
            glob_deg = degree["global"]
            ols = sm.OLS(loc_deg.astype(float), glob_deg.astype(float)).fit()
            degree["resid"] = ols.resid
            degree["fitted"] = ols.fittedvalues
            degree = degree.sort_values(by="resid", ascending=False)
        if iter_name is not None:
            degree["iter_name"] = iter_name
        return degree

    """ ----------------------------------------------------------------------
        Cluster Methods
    """

    def cluster_genes(self, cluster_id):
        """
            Return the genes that are in a cluster

            Parameters
            ----------
            cluster_id: str / int
                The ID of the cluster for which to get the gene IDs.
                Technically a string, but MCL clusters are assigned
                numbers. This is automatically converted so '0' == 0.

            Returns
            -------
            A list of Loci (genes) that are in the cluster
        """
        ids = self.clusters.query(f"cluster == {cluster_id}").index.values
        return self.refgen[ids]

    def cluster_coordinates(
        self, 
        cluster_number, 
        nstd=2,
        min_ratio=1.618
    ):
        """
            Calculate the rough coordinates around an MCL 
            cluster.

            Returns parameters that can be used to draw an ellipse.
            e.g. for cluster #5
            >>> from matplotlib.patches import Ellipse
            >>> e = Ellipse(**self.cluster_coordinates(5))

        """
        # Solution inspired by:
        # https://stackoverflow.com/questions/12301071/multidimensional-confidence-intervals
        # Get the coordinates of the MCL cluster
        coor = self.coordinates()
        gene_ids = [x.id for x in self.cluster_genes(cluster_number)]
        points = coor.loc[gene_ids]
        points = points.iloc[np.logical_not(np.isnan(points.x)).values, :]
        # Calculate stats for eigenvalues
        pos = points.mean(axis=0)
        cov = np.cov(points, rowvar=False)

        def eigsorted(cov):
            vals, vecs = np.linalg.eigh(cov)
            order = vals.argsort()[::-1]
            return vals[order], vecs[:, order]

        vals, vecs = eigsorted(cov)
        theta = np.degrees(np.arctan2(*vecs[:, 0][::-1]))
        width, height = 2 * nstd * np.sqrt(vals)
        if min_ratio:
            small_axis,big_axis = sorted([width,height])
            if big_axis / small_axis < min_ratio:
                small_axis = big_axis / min_ratio
            if width < height:
                width = small_axis
            else:
                height = small_axis
        return {"xy": pos, "width": width, "height": height, "angle": theta}

    def cluster_expression(self, 
        min_cluster_size=10, 
        max_cluster_size=10e10, 
        normalize=True
    ):
        """
            Get a matrix of cluster x accession gene expression.
            Each row represents the average gene expression in each accession
            for the genes in the cluster.

            Parameters
            ----------
            min_cluster_size : int (default:10)
                Clusters smaller than this will not be included in the
                expression matrix.
            normalize : bool (default:True)
                If true, each row will be standard normalized meaning that
                0 will represent the average (mean) across all accessions
                and the resultant values in the row will represent the number 
                of standard deviations from the mean.

            Returns
            -------
            A DataFrame containing gene expression values. Each row represents
            a cluster and each column represents an accession. The values of
            the matrix are the average gene expression (of genes in the cluster)
            for each accession.

        """
        # Extract clusters
        dm = (
            self.clusters.groupby("cluster")
            .filter(lambda x: len(x) >= min_cluster_size and len(x) <= max_cluster_size)
            .groupby("cluster")
            .apply(lambda x: self.expr(genes=self.refgen[x.index]).mean())
        )
        if normalize:
            dm = dm.apply(lambda x: (x - x.mean()) / x.std(), axis=1)
        if len(dm) == 0:
            self.log.warn("No clusters larger than {} ... skipping", min_cluster_size)
            return None
        return dm

    def import_coordinates_from_cyjs(
        self,
        cyjs_path,
        invert_y_coor=True
    ):
        '''
            Import node coordinates from a cyjs file.
           
            Parameters
            ----------
            cyjs_path : str (Pathlike)
                Path the cytoscape JSON file
            invert_y_coor : bool (default: True)
                If True, the y-coordinate will be inverted (y=-1*y).
                For some reason Cytoscape has an inverted y-coordinate
                system, toggling this will fix it.
        '''
        cyjs = json.load(open(cyjs_path,'r'))
        pos  = pd.DataFrame(
            [(n['data']['id_original'].upper(), n['position']['x'], n['position']['y']) \
                for n in cyjs['elements']['nodes']],
            columns=['gene','x','y']
        )
        if invert_y_coor:
            pos['y'] = -1*pos['y']
        index = pos['gene']
        pos = pos[['x','y']]
        pos.index = index
        self._bcolz("coordinates", df=pos)

    def coordinates(
            self, 
            method='spring',
            iterations=10, 
            force=False, 
            max_edges=10e100, 
            lcc_only=True,
        ):
        """ 
            Returns x,y coordinates for (a subset of) genes in the network. 
            If coordinates have not been previously calculated OR the force
            kwarg is True, gene coordinates will be calculated using the 
            ForceAtlas2 algorithm. NOTE: by default
        """
        from fa2 import ForceAtlas2

        pos = self._bcolz("coordinates")
        if pos is None or force == True:
            import scipy.sparse.csgraph as csgraph
            import networkx

            A, i = self.to_sparse_matrix(remove_orphans=False, max_edges=max_edges)
            # generate a reverse lookup for index to label
            rev_i = {v: k for k, v in i.items()}
            num, ccindex = csgraph.connected_components(A, directed=False)
            # convert to csc
            self.log(f"Converting to compressed sparse column")
            L = A.tocsc()
            if lcc_only:
                self.log("Extracting largest connected component")
                lcc_index, num = Counter(ccindex).most_common(1)[0]
                L = L[ccindex == lcc_index, :][:, ccindex == lcc_index]
                self.log(f"The largest CC has {num} nodes")
                # get labels based on index in L
                (lcc_indices,) = np.where(ccindex == lcc_index)
                labels = [rev_i[x] for x in lcc_indices]
            else:
                labels = [rev_i[x] for x in range(L.shape[0])]
            self.log("Calculating positions")
            if method == 'spring':
                coordinates = nx.layout._sparse_fruchterman_reingold(
                    L,
                    iterations=iterations
                )
            elif method == 'forceatlas2':
                forceatlas2 = ForceAtlas2(
                    # Behavior alternatives
                    outboundAttractionDistribution=True,
                    linLogMode=False,
                    adjustSizes=False,
                    edgeWeightInfluence=1.0,
                    # Performance
                    jitterTolerance=0.1,
                    barnesHutOptimize=True,
                    barnesHutTheta=0.6, #1.2,
                    multiThreaded=False,
                    # Tuning
                    scalingRatio=2.0,
                    strongGravityMode=True,
                    gravity=0.1,
                    # Logging
                    verbose=True,
                )
                coordinates = positions = forceatlas2.forceatlas2(
                    L, pos=None, iterations=iterations
                )
            pos = pd.DataFrame(coordinates)
            pos.index = labels
            pos.columns = ["x", "y"]
            self._bcolz("coordinates", df=pos)
        return pos

    """ ----------------------------------------------------------------------
        Plotting Methods
    """

    def plot_network(
        self,
        filename=None,
        target_genes=None,
        target_gene_alpha=0.5,
        ax=None,
        include_title=True,
        # coordinate kwargs 
        force=False,
        lcc_only=True,
        max_edges=None,
        min_degree=None,
        iterations=100, 
        # cluster kwargs
        draw_clusters=True,
        color_clusters=True,
        label_clusters=True,
        label_size=20,
        min_cluster_size=100,
        max_cluster_size=10e100,
        max_clusters=None,
        cluster_std=1,
        cluster_line_width=2,
        # style kwargs 
        node_size=20,
        edge_color='k',
        edge_alpha=0.7,
        draw_edges=False,
        background_color='#2196F3',#'xkcd:dark',
        foreground_color='#BD0000'#"xkcd:crimson"
    ):
        '''
            Plot a "hairball" image of the network.
        '''
        from matplotlib.colors import XKCD_COLORS
        xkcd = XKCD_COLORS.copy()
        coor = self.coordinates(lcc_only=lcc_only, force=force, iterations=iterations)
        # Filter by degree
        if min_degree is not None:
            coor =  coor.loc[self.degree.query(f'Degree >= {min_degree}').index]
        if ax is None:
            fig = plt.figure(facecolor="white", figsize=(8, 8))
            ax = fig.add_subplot(111)
        # Plot the background genes
        ax.set_facecolor("white")
        ax.grid(False)
        ax.set_xticks([])
        ax.set_yticks([])
        # Plot edges
        if draw_edges:
            self.log("Plotting edges")
            edges = self.subnetwork(
                gene_list=self.refgen.from_ids(
                    coor.index.values
                )
            ).reset_index()
            if max_edges is not None:
                max_edges = min(max_edges,len(edges))
                edges = edges.sort_values(by="score", ascending=False)[0:max_edges]
            # Extract the coordinates for edges
            a_coor = coor.loc[edges.gene_a]
            b_coor = coor.loc[edges.gene_b]
            #Plot using a matplotlib lines collection
            lines = LineCollection(
                zip(zip(a_coor.x,a_coor.y),zip(b_coor.x,b_coor.y)),
                colors=edge_color,
                antialiased=(1,),
                alpha=edge_alpha
            )
            lines.set_zorder(1)
            ax.add_collection(lines)

        ax.scatter(
            coor.x, 
            coor.y, 
            alpha=1, 
            color=background_color,  #xkcd.pop(background_color),
            s=node_size
        )
        # Plot the genes
        if target_genes is not None:
            self.log("Plotting genes")
            ids = coor.loc[[x.id for x in target_genes if x.id in coor.index]]
            nodes = ax.scatter(
                ids.x, 
                ids.y, 
                color=foreground_color,
                s=node_size,
                alpha=target_gene_alpha
            )
            nodes.set_zorder(2)
        # Plot clusters
        if draw_clusters:
            from matplotlib.patches import Ellipse
            big_clusters = [
                k
                for k, v in Counter(self.clusters.cluster).items()
                if v > min_cluster_size
                and v < max_cluster_size
            ]
            # define cluster colors
            cluster_colors = list(xkcd.values())
            for i, clus in enumerate(big_clusters):
                if max_clusters is not None and i + 1 > max_clusters:
                    break
                ids = [x.id for x in self.cluster_genes(clus) if x.id in coor.index]
                ccoor = coor.loc[ids]
                if color_clusters:
                    # This will overwrite the genes in the cluster giving them colors 
                    ax.scatter(
                        ccoor.x, 
                        ccoor.y,
                        s=node_size,
                        color=cluster_colors[i]
                    )
                try:
                    c = self.cluster_coordinates(
                        clus,
                        nstd=cluster_std
                    )
                except (KeyError,np.linalg.LinAlgError) as e:
                    continue
                c.update(
                    {
                        "edgecolor": "black",
                        "fill"     : False,
                        "linestyle": ":",
                        "linewidth": cluster_line_width,
                    }
                )
                e = Ellipse(**c)
                ax.add_artist(e)
                if label_clusters:
                    ax.annotate(
                        clus,
                        size=label_size,
                        xy=(c['xy']['x'],c['xy']['y']),
                        bbox=dict(boxstyle="round", fc="w")
                    )
        if include_title:
            ax.set_title(self.name,size='large')
        if filename is not None:
            plt.savefig(filename)
        return ax

    def plot_heatmap(
        self,
        filename=None,
        ax=None,
        genes=None,
        accessions=None,
        gene_normalize=True,
        raw=False,
        cluster_method="ward",
        include_accession_labels=None,
        include_gene_labels=None,
        avg_by_cluster=False,
        min_cluster_size=10,
        max_cluster_size=10e10,
        cluster_accessions=True,
        plot_dendrogram=True,
        nan_color=None,
        cmap=None,
        expr_boundaries=3.5,
        figsize=(20,20)
    ):
        """
            Plots a heatmap of genes x expression.

            Parameters
            ----------
            filename : str 
                If specified, figure will be written to output filename
            genes : co.Locus iterable (default: None)
                An iterable of genes to plot expression for
            accessions : iterable of str
                An iterable of strings to extract for expression values.
                Values must be a subset of column values in expression matrix
            gene_normalize: bool (default: True)
                normalize gene values in heatmap to show expression patterns.
            raw : bool (default: False)
                If true, raw expression data will be used. Default is to use
                the normailzed, QC'd data.
            cluster_method : str (default: 'single')
                Specifies how to organize the gene axis in the heatmap. If
                'mcl', genes will be organized by MCL cluster. Otherwise
                the value must be one of the linkage methods defined by 
                the scipy.cluster.hierarchy.linkage function: [single,
                complete, average, weighted, centroid, median, ward].
                https://docs.scipy.org/doc/scipy/reference/generated/scipy.cluster.hierarchy.linkage.html
            include_accession_labels : bool (default: None)
                Force the rendering of accession labels. If None, accession 
                lables will be included as long as there are less than 30.
            include_gene_lables : bool (default: None)
                Force rendering of gene labels in heatmap. If None, gene
                labels will be rendered as long as there are less than 100.
            avg_by_cluster : bool (default: False)
                If True, gene expression values will be averaged by MCL cluster
                showing a single row per cluster.
            min_cluster_size : int ( default: 10)
                If avg_by_cluster, only cluster sizes larger than min_cluster_size
                will be included.
            cluster_accessions : bool (default: True)
                If true, accessions will be clustered
            plot_dendrogram : bool (default: True)
                If true, dendrograms will be plotted
            nan_color : str (default: None)
                Specifies the color of nans in the heatmap. Changing this
                to a high contrast color can help identify problem areas.
                If not specified, nans will be the middle (neutral) value
                in the heatmap.
            cmap : str (default: 'viridis')
                A matplotlib color map for the heatmap. See
                https://matplotlib.org/gallery/color/colormap_reference.html
                for options.
            expr_boundaries : int (default: 3)
                Set the min/max boundaries for expression values so that
                the cmap colors aren't dominated by outliers

            Returns
            -------
            a populated matplotlib figure object

        """
        # These are valid hierarchical clustering methods
        hier_cluster_methods = [
            "single",
            "complete",
            "average",
            "weighted",
            "centroid",
            "median",
            "ward",
        ]
        # Get the Expressiom Matrix
        if avg_by_cluster == True:
            dm = self.cluster_expression(
                min_cluster_size=min_cluster_size,
                max_cluster_size=max_cluster_size,
                normalize=True
            )
        else:
            # Fetch the Expr Matrix
            dm = self.expr(
                genes=genes,
                accessions=accessions,
                raw=raw,
                gene_normalize=gene_normalize,
            )
        # set the outliers to the maximium value for the heatmap
        dm[dm > expr_boundaries] = expr_boundaries
        dm[dm < -1*expr_boundaries] = -1 * expr_boundaries
        # Get the Gene clustering order
        if cluster_method in hier_cluster_methods:
            self.log("Ordering rows by leaf")
            expr_linkage = fastcluster.linkage(dm.fillna(0), method=cluster_method)
            order = leaves_list(expr_linkage)
            dm = dm.iloc[order, :]
        elif cluster_method == "mcl":
            self.log("Ordering rows by MCL cluster")
            order = (
                self.clusters.loc[dm.index]
                .fillna(np.inf)
                .sort_values(by="cluster")
                .index.values
            )
            dm = dm.loc[order, :]
        else:
            # No cluster order.
            self.log("Unknown gene ordering: {}, no ordering performed", cluster_method)

        # Get leaves of accessions
        if cluster_accessions:
            if cluster_method == "mcl":
                acc_clus_method = "ward"
            else:
                acc_clus_method = cluster_method
            accession_linkage = fastcluster.linkage(
                dm.fillna(0).T, method=acc_clus_method
            )
            # Re-order the matrix based on tree
            order = leaves_list(accession_linkage)
            dm = dm.iloc[:, order]


        # Save plot if provided filename
        if ax is None:
            fig = plt.figure(facecolor="white", figsize=figsize,constrained_layout=True)
            ax = fig.add_subplot(111)
        if plot_dendrogram == True:
            gs = fig.add_gridspec(
                2, 2, height_ratios=[3, 1], width_ratios=[3, 1], hspace=0, wspace=0
            )
            ax = plt.subplot(gs[0])
            # make the axes for the dendrograms
            gene_ax = plt.subplot(gs[1])
            gene_ax.set_xticks([])
            gene_ax.set_yticks([])
            accession_ax = plt.subplot(gs[2])
        # Plot the Expression matrix
        nan_mask = np.ma.array(dm, mask=np.isnan(dm))
        if cmap is None:
            cmap = self._cmap
        else:
            cmap = plt.get_cmap(cmap)
        # Set the nan color to the middle unless a color is specifid
        if nan_color is None:
            nan_color = cmap(0.5)
        cmap.set_bad(nan_color, 1.0)
        vmax = max(np.nanmin(abs(dm)), np.nanmax(abs(dm)))
        vmin = vmax * -1
        im = ax.matshow(nan_mask, aspect="auto", cmap=cmap, vmax=vmax, vmin=vmin)
        # Intelligently add labels
        ax.grid(False)
        ax.tick_params(labelsize=8)
        if (
            (include_accession_labels is None and len(dm.columns) < 60)
             or include_accession_labels == True
        ):
            ax.set(xticklabels=dm.columns.values, yticklabels=dm.index.values)
            ax.tick_params("x", labelrotation=45)
            for label in ax.get_xticklabels():
                label.set_horizontalalignment('left')
            ax.set(xticks=np.arange(len(dm.columns)))
        if (
            (include_gene_labels is None and len(dm.index) < 100)
             or include_gene_labels == True
        ):
            ax.set(yticks=np.arange(len(dm.index)))
        fig.align_labels()
        # ax.figure.colorbar(im)
        if plot_dendrogram == True:
            with plt.rc_context({"lines.linewidth": 1.0}):
                from scipy.cluster import hierarchy

                hierarchy.set_link_color_palette(["k"])

                # Plot the accession dendrogram
                import sys
                if cluster_accessions == True:
                    sys.setrecursionlimit(10000)
                    dendrogram(
                        accession_linkage,
                        ax=accession_ax,
                        color_threshold=np.inf,
                        orientation="bottom",
                    )
                    accession_ax.set_facecolor("w")
                    accession_ax.set_xticks([])
                    accession_ax.set_yticks([])
                # Plot the gene dendrogram
                if cluster_method in hier_cluster_methods:
                    dendrogram(
                        expr_linkage,
                        ax=gene_ax,
                        orientation="right",
                        color_threshold=np.inf,
                    )
                    gene_ax.set_xticks([])
                    gene_ax.set_yticks([])
                    gene_ax.set_facecolor("w")
        # Save if you wish
        if filename is not None:
            plt.savefig(filename, dpi=300, figsize=figsize)
            plt.close()
        return ax.figure

    def plot_scores(self, filename=None, pcc=True, bins=50):
        """
            Plot the histogram of PCCs.

            Parameters
            ----------
            filename : str (default: None)
                The output filename, if none will return the matplotlib object
            pcc : bool (default:True)
                flag to convert scores to pccs
            bins : int (default: 50)
                the number of bins in the histogram
        """
        fig = plt.figure(figsize=(8, 6))
        # grab the scores only and put in a
        # np array to save space (pandas DF was HUGE)
        scores = odo(self.coex.score, np.ndarray)[~np.isnan(self.coex.score)]
        if pcc:
            self.log("Transforming scores")
            scores = (scores * float(self._global("pcc_std"))) + float(
                self._global("pcc_mean")
            )
            # Transform Z-scores to pcc scores (inverse fisher transform)
            scores = np.tanh(scores)
        plt.hist(scores, bins=bins)
        plt.xlabel("PCC") if pcc else plt.xlabel("Z-Score")
        plt.ylabel("Freq")
        if filename is not None:
            plt.savefig(filename)
            plt.close()
        else:
            return fig

    def compare_degree(self, obj, diff_genes=10, score_cutoff=3):
        """
            Compares the degree of one COB to another.

            Parameters
            ----------
            obj : COB instance
                The object you are comparing the degree to.
            diff_genes : int (default: 10)
                The number of highest and lowest different
                genes to report
            score_cutoff : int (default: 3)
                The edge score cutoff used to called
                significant.
        """
        self.log("Comparing degrees of {} and {}", self.name, obj.name)

        # Put the two degree tables in the same table
        lis = pd.concat(
            [self.degree.copy(), obj.degree.copy()], axis=1, ignore_index=True
        )

        # Filter the table of entries to ones where both entries exist
        lis = lis[(lis[0] > 0) & (lis[1] > 0)]
        delta = lis[0] - lis[1]

        # Find the stats beteween the two sets,
        # and the genes with the biggest differences
        delta.sort_values(ascending=False, inplace=True)
        highest = sorted(
            list(dict(delta[:diff_genes]).items()), key=lambda x: x[1], reverse=True
        )
        lowest = sorted(
            list(dict(delta[-diff_genes:]).items()), key=lambda x: x[1], reverse=False
        )
        ans = {
            "correlation_between_cobs": lis[0].corr(lis[1]),
            "mean_of_difference": delta.mean(),
            "std_of_difference": delta.std(),
            ("bigger_in_" + self.name): highest,
            ("bigger_in_" + obj.name): lowest,
        }
        return ans

    """ ----------------------------------------------------------------------
            Internal Methods
    """

    def _calculate_coexpression(self, significance_thresh=3):
        """
            Generates pairwise PCCs for gene expression profiles in self._expr.
            Also calculates pairwise gene distance.
        """
        # 1. Calculate the PCCs
        self.log("Calculating Coexpression")
        num_bytes_needed = comb(self.shape()[0], 2) * 8
        if num_bytes_needed > psutil.virtual_memory().available:
            raise MemoryError("Not enough RAM to calculate co-expression network")
        # pass in a contigious array to the cython function to calculate PCCs
        pccs = PCCUP.pair_correlation(
            np.ascontiguousarray(
                # PCCUP expects floats
                self._expr.as_matrix().astype("float")
            )
        )

        self.log("Applying Fisher Transform")
        pccs[pccs >= 1.0] = 0.9999999
        pccs[pccs <= -1.0] = -0.9999999
        pccs = np.arctanh(pccs)
        gc.collect()

        # Do a PCC check to make sure they are not all NaNs
        if not any(np.logical_not(np.isnan(pccs))):
            raise ValueError(
                "Not enough data is available to reliably calculate co-expression, "
                "please ensure you have more than 10 accessions to calculate correlation coefficient"
            )

        self.log("Calculating Mean and STD")
        # Sometimes, with certain datasets, the NaN mask overlap
        # completely for the two genes expression data making its PCC a nan.
        # This affects the mean and std fro the gene.
        pcc_mean = np.ma.masked_array(pccs, np.isnan(pccs)).mean()
        self._global("pcc_mean", pcc_mean)
        gc.collect()
        pcc_std = np.ma.masked_array(pccs, np.isnan(pccs)).std()
        self._global("pcc_std", pcc_std)
        gc.collect()

        # 2. Calculate Z Scores
        self.log("Finding adjusted scores")
        pccs = (pccs - pcc_mean) / pcc_std
        gc.collect()

        # 3. Build the dataframe
        self.log("Build the dataframe and set the significance threshold")
        self._global("significance_threshold", significance_thresh)
        raw_coex = self._raw_coex(pccs, significance_thresh)
        del pccs
        gc.collect()

        # 4. Calculate Gene Distance
        self.log("Calculating Gene Distance")
        raw_coex.addcol(
            self.refgen.pairwise_distance(
                gene_list=self.refgen.from_ids(self._expr.index)
            ),
            pos=1,
            name="distance",
        )
        gc.collect()

        # 5. Cleanup
        raw_coex.flush()
        del raw_coex
        gc.collect()

        # 6. Load the new table into the object
        self.coex = self._bcolz("coex", blaze=True)
        self.set_sig_edge_zscore(float(self._global("significance_threshold")))
        self.log("Done")
        return self

    def _calculate_degree(self, update_db=True):
        """
            Calculates degrees of genes within network. 
        """
        self.log("Building Degree")
        # Get significant expressions and dump coex from memory for time being
        # Generate a df that starts all genes at 0
        names = self._expr.index.values
        self.degree = pd.DataFrame(0, index=names, columns=["Degree"])
        # Get the index and find the counts
        self.log("Calculating Gene degree")
        sigs = np.arange(len(self.coex))[odo(self.coex.significant, np.ndarray)]
        sigs = PCCUP.coex_expr_index(sigs, len(self._expr.index.values))
        sigs = list(Counter(chain(*sigs)).items())
        if len(sigs) > 0:
            # Translate the expr indexes to the gene names
            for i, degree in sigs:
                self.degree.ix[names[i]] = degree
        # Update the database
        if update_db:
            self._bcolz("degree", df=self.degree)
        # Cleanup
        del sigs
        del names
        gc.collect()
        return self

    def _calculate_gene_hierarchy(self, method="single"):
        """
            Calculate the hierarchical gene distance for the Expr matrix
            using the coex data.

            Notes
            -----
            This is kind of expenive.
        """
        import fastcluster

        # We need to recreate the original PCCs
        self.log("Calculating hierarchical clustering using {}".format(method))
        if len(self.coex) == 0:
            raise ValueError("Cannot calculate leaves without coex")
        pcc_mean = float(self._global("pcc_mean"))
        pcc_std = float(self._global("pcc_std"))
        # Get score column and dump coex from memory for time being
        dists = odo(self.coex.score, np.ndarray)
        # Subtract pccs from 1 so we do not get negative distances
        dists = (dists * pcc_std) + pcc_mean
        dists = np.tanh(dists)
        dists = 1 - dists
        # convert nan to 0's, linkage can only use finite values
        dists[np.isnan(dists)] = 0
        gc.collect()
        # Find the leaves from hierarchical clustering
        gene_link = fastcluster.linkage(dists, method=method)
        return gene_link

    def _calculate_leaves(self, method="single"):
        """
            This calculates the leaves of the dendrogram from the coex
        """
        gene_link = self._calculate_gene_hierarchy(method=method)
        self.log("Finding the leaves")
        leaves = leaves_list(gene_link)
        gc.collect()

        # Put them in a dataframe and stow them
        self.leaves = pd.DataFrame(leaves, index=self._expr.index, columns=["index"])
        self._gene_link = gene_link
        self._bcolz("leaves", df=self.leaves)

        # Cleanup and reinstate the coex table
        gc.collect()
        return self

    def _calculate_clusters(self):
        """
            Calculates global clusters
        """
        clusters = self.mcl()
        self.log("Building cluster dataframe")
        names = self._expr.index.values
        self.clusters = pd.DataFrame(np.nan, index=names, columns=["cluster"])
        if len(clusters) > 0:
            self.clusters = pd.DataFrame(
                data=[
                    (gene.id, i)
                    for i, cluster in enumerate(clusters)
                    for gene in cluster
                ],
                columns=["Gene", "cluster"],
            ).set_index("Gene")
            self._bcolz("clusters", df=self.clusters)
        self.log("Creating Cluster Ontology")
        terms = []
        for i, x in enumerate(self.clusters.groupby("cluster")):
            genes = self.refgen[x[1].index.values]
            terms.append(
                Term(
                    "MCL{}".format(i),
                    desc="{} MCL Cluster {}".format(self.name, i),
                    loci=genes,
                )
            )
        self.MCL = Ontology.from_terms(
            terms,
            "{}MCL".format(self.name),
            "{} MCL Clusters".format(self.name),
            self.refgen,
        )
        self.log("Finished finding clusters")
        return self

    def _coex_concordance(self, gene_a, gene_b, maxnan=10, return_dict=False):
        """
            This is a sanity method to ensure that the pcc calculated
            directly from the expr profiles matches the one stored in
            the database
        """
        expr_a = self.expr_profile(gene_a).values
        expr_b = self.expr_profile(gene_b).values
        mask = np.logical_and(np.isfinite(expr_a), np.isfinite(expr_b))
        if sum(mask) < maxnan:
            # too many nans to reliably calculate pcc
            return np.nan
        r = pearsonr(expr_a[mask], expr_b[mask])[0]
        # fisher transform it
        z = np.arctanh(r - 0.0000001)
        # standard normalize it
        z = (z - float(self._global("pcc_mean"))) / float(self._global("pcc_std"))
        if return_dict:
            return {'pearsonr': r, 'zscore': z}
        else:
            return z

    def _sparse_fruchterman_reingold(
            self,
            A, 
            k=None, 
            pos=None, 
            fixed=None, 
            iterations=50,
            threshold=1e-4, 
            seed=42
        ):
        '''
            This code was modified from the NetworkX algorithm for 
            sparse_fruchterman_reingold spring embedded algorithm.

            See the following page for details on the source:
            https://github.com/networkx/networkx/blob/15e17c0a2072ea56df3d9cd9152ee682203e8cd9/networkx/drawing/layout.py#L502

            =======
            NetworkX is distributed with the 3-clause BSD license.

            ::

               Copyright (C) 2004-2020, NetworkX Developers
               Aric Hagberg <hagberg@lanl.gov>
               Dan Schult <dschult@colgate.edu>
               Pieter Swart <swart@lanl.gov>
               All rights reserved.

               Redistribution and use in source and binary forms, with or without
               modification, are permitted provided that the following conditions are
               met:

                 * Redistributions of source code must retain the above copyright
                   notice, this list of conditions and the following disclaimer.

                 * Redistributions in binary form must reproduce the above
                   copyright notice, this list of conditions and the following
                   disclaimer in the documentation and/or other materials provided
                   with the distribution.

                 * Neither the name of the NetworkX Developers nor the names of its
                   contributors may be used to endorse or promote products derived
                   from this software without specific prior written permission.

               THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
               "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
               LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
               A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
               OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
               SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
               LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
               DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
               THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
               (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
               OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
        '''
        try:
            nnodes, _ = A.shape
        except AttributeError:
            msg = "fruchterman_reingold() takes an adjacency matrix as input"
            raise ValueError(msg)

        # Create random positions based on the seed
        if pos is None:
            # random initial positions
            pos = np.asarray(
                np.random.RandomState().rand(nnodes,2),
                dtype=A.dtype
            )
        else:
            # make sure positions are of same type as matrix
            pos = pos.astype(A.dtype)

        # optimal distance between nodes
        if k is None:
            k = np.sqrt(1.0 / nnodes)
        # the initial "temperature"  is about .1 of domain area (=1x1)
        # this is the largest step allowed in the dynamics.
        # We need to calculate this in case our fixed positions force our domain
        # to be much bigger than 1x1
        t = max(max(pos.T[0]) - min(pos.T[0]), max(pos.T[1]) - min(pos.T[1])) * 0.1
        # simple cooling scheme.
        # linearly step down by dt on each iteration so last iteration is size dt.
        dt = t / float(iterations + 1)
        delta = np.zeros((pos.shape[0], pos.shape[0], pos.shape[1]), dtype=A.dtype)
        # the inscrutable (but fast) version
        # this is still O(V^2)
        # could use multilevel methods to speed this up significantly
        for iteration in range(iterations):
            self.log(f"On iteration {iteration}")
            # matrix of difference between points
            delta = pos[:, np.newaxis, :] - pos[np.newaxis, :, :]
            # distance between points
            distance = np.linalg.norm(delta, axis=-1)
            # enforce minimum distance of 0.01
            np.clip(distance, 0.01, None, out=distance)
            # displacement "force"
            displacement = np.einsum('ijk,ij->ik',
                                     delta,
                                     (k * k / distance**2 - A * distance / k))
            # update positions
            length = np.linalg.norm(displacement, axis=-1)
            length = np.where(length < 0.01, 0.1, length)
            delta_pos = np.einsum('ij,i->ij', displacement, t / length)
            if fixed is not None:
                # don't change positions of fixed nodes
                delta_pos[fixed] = 0.0
            pos += delta_pos
            # cool temperature
            t -= dt
            err = np.linalg.norm(delta_pos) / nnodes
            if err < threshold:
                break
        return pos


    """ -----------------------------------------------------------------------
            Class Methods -- Factory Methods
    """

    @classmethod
    def create(cls, name, description, refgen):
        """
        """
        self = super().create(name, description, refgen)
        self._bcolz("gene_qc_status", df=pd.DataFrame())
        self._bcolz("accession_qc_status", df=pd.DataFrame())
        self._bcolz("coex", df=pd.DataFrame())
        self._bcolz("degree", df=pd.DataFrame())
        self._bcolz("mcl_cluster", df=pd.DataFrame())
        self._bcolz("leaves", df=pd.DataFrame())
        self._expr_index = defaultdict(
            lambda: None, {gene: index for index, gene in enumerate(self._expr.index)}
        )
        return self

    @classmethod
    def from_Expr(cls, expr, zscore_cutoff=3, **kwargs):
        """
            Create a COB instance from an camoco.Expr (Expression) instance.
            A COB inherits all the methods of a Expr instance and implements
            additional coexpression specific methods. This method accepts an
            already build Expr instance and then performs the additional
            computations needed to build a full fledged COB instance.

            Parameters
            ----------
            expr : camoco.Expr
                The camoco expression object used to build the 
                co-expression network.
            zscore_cutoff : int (defualt: 3)
                The zscore cutoff for the network.

            Returns
            -------
            camoco.COB instance

        """
        # The Expr object already exists, just get a handle on it
        self = expr
        self._calculate_coexpression()
        self._calculate_degree()
        self._calculate_leaves()
        self._calculate_clusters()
        return self

    @classmethod
    def from_DataFrame(
        cls, df, name, description, refgen, rawtype=None, zscore_cutoff=3, **kwargs
    ):
        """
            The method will read the table in (as a pandas dataframe),
            build the Expr object passing all keyword arguments in ``**``kwargs
            to the classmethod Expr.from_DataFrame(...). See additional
            ``**``kwargs in COB.from_Expr(...)

            Parameters
            ----------
            df : pandas.DataFrame
                A Pandas dataframe containing the expression information.
                Assumes gene names are in the index while accessions
                (experiments) are stored in the columns.
            name : str
                Name of the dataset stored in camoco database
            description : str
                Short string describing the dataset
            refgen : camoco.RefGen
                A Camoco refgen object which describes the reference
                genome referred to by the genes in the dataset. This
                is cross references during import so we can pull information
                about genes we are interested in during analysis.
            rawtype : str (default: None)
                This is noted here to reinforce the impotance of the rawtype
                passed to camoco.Expr.from_DataFrame. See docs there
                for more information.
            zscore_cutoff : int (defualt: 3)
                The zscore cutoff for the network.
            \*\*kwargs : key,value pairs
                additional parameters passed to subsequent methods.
                (see Expr.from_DataFrame)

        """
        # Create a new Expr object from a data frame
        expr = super().from_DataFrame(
            df,
            name,
            description,
            refgen,
            rawtype,
            zscore_cutoff=zscore_cutoff,
            **kwargs,
        )
        return cls.from_Expr(expr)

    @classmethod
    def from_table(
        cls,
        filename,
        name,
        description,
        refgen,
        rawtype=None,
        sep="\t",
        index_col=None,
        zscore_cutoff=3,
        **kwargs,
    ):
        """
            Build a COB Object from an FPKM or Micrarray CSV. This is a
            convenience method which handles reading in of tables.
            Files need to have gene names as the first column and
            accession (i.e. experiment) names as the first row. All
            kwargs will be passed to COB.from_DataFrame(...). See
            docstring there for option descriptions.

            Parameters
            ----------
            filename : str (path)
                the path to the FPKM table in csv or tsv
            name : str
                Name of the dataset stored in camoco database
            description : str
                Short string describing the dataset
            refgen : camoco.RefGen
                A Camoco refgen object which describes the reference
                genome referred to by the genes in the dataset. This
                is cross references during import so we can pull information
                about genes we are interested in during analysis.
            rawtype : str (default: None)
                This is noted here to reinforce the importance of the rawtype
                passed to camoco.Expr.from_DataFrame. See docs there for
                more information.
            sep : str (default: \\t)
                Specifies the delimiter of the file referenced by the
                filename parameter.
            index_col : str (default: None)
                If not None, this column will be set as the gene index
                column. Useful if there is a column name in the text file
                for gene names.
            zscore_cutoff : int (defualt: 3)
                The zscore cutoff for the network.
            **kwargs : key value pairs
                additional parameters passed to subsequent methods.

            Returns
            -------
                a COB object
        """
        df = pd.read_table(filename, sep=sep, compression="infer", index_col=index_col)
        return cls.from_DataFrame(
            df,
            name,
            description,
            refgen,
            rawtype=rawtype,
            zscore_cutoff=zscore_cutoff,
            **kwargs,
        )
    


def fix_val(val):
    if isinf(val):
        return -1
    if np.isnan(val):
        # because Fuck JSON
        return "null"
    else:
        return val





