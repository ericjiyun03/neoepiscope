#!/usr/bin/env python
"""
nexepi

Identifies neoepitopes from DNA-seq, VCF, GTF, and Bowtie index.
"""
import bisect
import argparse
import bowtie_index
import sys
import math
import string
import copy
import pickle
import copy
import os
import random
import re
import collections
import tempfile
import subprocess
import string
from operator import itemgetter
from sortedcontainers import SortedDict
from intervaltree import Interval, IntervalTree
#import Hapcut2interpreter as hap

_revcomp_translation_table = string.maketrans('ATCG', 'TAGC')

# X below denotes a stop codon
_codon_table = {
        "TTT":"F", "TTC":"F", "TTA":"L", "TTG":"L",
        "TCT":"S", "TCC":"S", "TCA":"S", "TCG":"S",
        "TAT":"Y", "TAC":"Y", "TAA":"X", "TAG":"X",
        "TGT":"C", "TGC":"C", "TGA":"X", "TGG":"W",
        "CTT":"L", "CTC":"L", "CTA":"L", "CTG":"L",
        "CCT":"P", "CCC":"P", "CCA":"P", "CCG":"P",
        "CAT":"H", "CAC":"H", "CAA":"Q", "CAG":"Q",
        "CGT":"R", "CGC":"R", "CGA":"R", "CGG":"R",
        "ATT":"I", "ATC":"I", "ATA":"I", "ATG":"M",
        "ACT":"T", "ACC":"T", "ACA":"T", "ACG":"T",
        "AAT":"N", "AAC":"N", "AAA":"K", "AAG":"K",
        "AGT":"S", "AGC":"S", "AGA":"R", "AGG":"R",
        "GTT":"V", "GTC":"V", "GTA":"V", "GTG":"V",
        "GCT":"A", "GCC":"A", "GCA":"A", "GCG":"A",
        "GAT":"D", "GAC":"D", "GAA":"E", "GAG":"E",
        "GGT":"G", "GGC":"G", "GGA":"G", "GGG":"G"
    }
_complement_table = string.maketrans("ATCG", "TAGC")

_help_intro = """neoscan searches for neoepitopes in seq data."""
def help_formatter(prog):
    """ So formatter_class's max_help_position can be changed. """
    return argparse.HelpFormatter(prog, max_help_position=40)

def gtf_to_cds(gtf_file, dictdir, pickle_it=True):
    """ References cds_dict to get cds bounds for later Bowtie query

        Keys in the dictionary are transcript IDs, while entries are lists of
            relevant CDS/stop codon data
            Data: [chromosome, start, stop, +/- strand]
        Writes cds_dict as a pickled dictionary

        gtf_file: input gtf file to process
        dictdir: path to directory to store pickled dicts

        Return value: dictionary
    """
    cds_dict = {}
    # Parse GTF to obtain CDS/stop codon info
    with open(gtf_file, "r") as f:
        for line in f:
            if line[0] != '#':
                tokens = line.strip().split('\t')
                if tokens[2] == "exon":
                    transcript_id = re.sub(
                                r'.*transcript_id \"([A-Z0-9._]+)\"[;].*', 
                                r'\1', tokens[8]
                                )
                    # Create new dictionary entry for new transcripts
                    if transcript_id not in cds_dict:
                        cds_dict[transcript_id] = [[tokens[0].replace(
                                                                    "chr", ""), 
                                                        int(tokens[3]), 
                                                        int(tokens[4]), 
                                                        tokens[6]]]
                    else:
                        cds_dict[transcript_id].append([tokens[0].replace(
                                                                    "chr", ""), 
                                                            int(tokens[3]), 
                                                            int(tokens[4]), 
                                                            tokens[6]])
    # Sort cds_dict coordinates (left -> right) for each transcript                                
    for transcript_id in cds_dict:
            cds_dict[transcript_id].sort(key=lambda x: x[0])
    # Write to pickled dictionary
    if pickle_it:
        pickle_dict = "".join([dictdir, "/", "transcript_to_CDS.pickle"])
        with open(pickle_dict, "wb") as f:
            pickle.dump(cds_dict, f)
    return cds_dict

def cds_to_tree(cds_dict, dictdir, pickle_it=True):
    """ Creates searchable tree of chromosome intervals from CDS dictionary

        Each chromosome is stored in the dictionary as an interval tree object
            Intervals are added for each CDS, with the associated transcript ID
            Assumes transcript is all on one chromosome - does not work for
                gene fusions
        Writes the searchable tree as a pickled dictionary

        cds_dict: CDS dictionary produced by gtf_to_cds()

        Return value: searchable tree
    """
    searchable_tree = {}
    # Add genomic intervals to the tree for each transcript
    for transcript_id in cds_dict:
        transcript = cds_dict[transcript_id]
        chrom = transcript[0][0]
        # Add new entry for chromosome if not already encountered
        if chrom not in searchable_tree:
            searchable_tree[chrom] = IntervalTree()
        # Add CDS interval to tree with transcript ID
        for cds in transcript:
            start = cds[1]
            stop = cds[2]
            # Interval coordinates are inclusive of start, exclusive of stop
            if stop > start:
                searchable_tree[chrom][start:stop] = transcript_id
            # else:
                # report an error?
    # Write to pickled dictionary
    if pickle_it:
        pickle_dict = "".join([dictdir, "/", "intervals_to_transcript.pickle"])
        with open(pickle_dict, "wb") as f:
            pickle.dump(searchable_tree, f)
    return searchable_tree

def get_transcripts_from_tree(chrom, start, stop, cds_tree):
    """ Uses cds tree to btain transcript IDs from genomic coordinates
            
        chrom: (String) Specify chrom to use for transcript search.
        start: (Int) Specify start position to use for transcript search.
        stop: (Int) Specify ending position to use for transcript search
        cds_tree: (Dict) dictionary of IntervalTree() objects containing
            transcript IDs as function of exon coords indexed by chr/contig ID.
            
        Return value: (set) a set of matching unique transcript IDs.
    """
    transcript_ids = set()
    # Interval coordinates are inclusive of start, exclusive of stop
    cds = cds_tree[chrom].search(start, stop)
    for cd in cds:
        if cd.data not in transcript_ids:
            transcript_ids.add(cd.data)
    return transcript_ids

def adjust_tumor_column(in_vcf, out_vcf):
    """ Swaps the sample columns in a somatic vcf

        HAPCUT2 only takes data from the first VCF sample column, so if the 
            tumor sample data is in the second VCF sample column, it must be
            swapped prior to optional germline merging or running HAPCUT2

        in_vcf: input vcf that needs the tumor sample data flipped
        out_vcf: output vcf to have the correct columns

        No return value.
    """
    header_lines = []
    other_lines = []
    # Process input vcf
    with open(in_vcf, "r") as f:
        for line in f:
            # Preserve header lines with out change
            if line[0:2] == "##":
                header_lines.append(line.strip("\n"))
            # Adjust column header and variant lines
            else:
                tokens = line.strip("\n").split("\t")
                new_line = "\t".join([tokens[0], tokens[1], tokens[2], 
                                        tokens[3], tokens[4], tokens[5], 
                                        tokens[6], tokens[7], tokens[8], 
                                        tokens[10], tokens[9]])
                other_lines.append(new_line)
    # Write new vcf
    with open(out_vcf, "w") as f:
        for line in header_lines:
            f.write(line + "\n")
        for line in other_lines:
            f.write(line + "\n")

def combinevcf(vcf1, vcf2, outfile="Combined.vcf"):
    """ Combines VCFs
        
        #### WE NEED TO ADJUST THIS ####
        ## Where are header lines going? ##
        ## Position of tumor vs. normal in somatic vcf is variable ##

        No return value.
    """
    vcffile = open(vcf2, "r")
    temp = open(vcf2 + ".tumortemp", "w+");
    header = open(vcf2 + ".header", "w+");
    for lines in vcffile:
        if (lines[0] != '#'):
            temp.write(lines)
        else:
            header.write(lines)
    vcffile.close()
    temp.close()
    header.close()
    vcffile = open(vcf1, "r")
    temp = open(vcf2 + ".germlinetemp", "w+");
    for lines in vcffile:
        if (lines[0] != '#'):
            temp.write(lines)
    vcffile.close()
    temp.close()    
    markgermline = "".join(['''awk '{print $0"*"}' ''', vcf2, 
                            ".germlinetemp > ", vcf2, ".germline"])
    marktumor    = "".join(['''awk '{print $0}' ''', vcf2, 
                            ".tumortemp > ", vcf2, ".tumor"])
    subprocess.call(markgermline, shell=True)
    subprocess.call(marktumor, shell=True)
    command = "".join(["cat ", vcf2, ".germline ", vcf2, ".tumor > ", 
                        vcf2, ".combine1"])
    subprocess.call(command, shell=True)
    command2 = "".join(["sort -k1,1 -k2,2n ", vcf2, ".combine1 > ", 
                        vcf2, ".sorted"])
    subprocess.call(command2, shell=True)
    command3 = "".join(["cat ", vcf2, ".header ", vcf2, ".sorted > ", 
                        vcf2, ".combine2"])
    subprocess.call(command3, shell=True)
    cut = "".join(["cut -f1,2,3,4,5,6,7,8,9,10 ", vcf2, 
                    ".combine2 > ", outfile])
    subprocess.call(cut, shell=True)
    for file in [".tumortemp", ".germlinetemp", ".combine1", ".combine2", 
                    ".sorted", ".tumor", ".germline", ".header"]:
        cleanup = "".join(["rm ", vcf2, file])
        subprocess.call(cleanup,shell=True)

def which(path):
    """ Searches for whether executable is present and returns version

        path: path to executable

        Return value: None if executable not found, else string with software
            name and version number
    """
    try:
        subprocess.check_call([path])
    except OSError as e:
        return None
    else:
        return path

def get_VAF_pos(VCF):
    """ Obtains position in VCF format/genotype fields of VAF

        VCF: path to input VCF

        Return value: None if VCF does not contain VAF, 
                        otherwise position of VAF
    """
    VAF_check = False
    with open(VCF) as f:
        for line in f:
            # Check header lines to see if FREQ exits in FORMAT fields
            if line[0] == "#":
                if "FREQ" in line:
                    VAF_check = True
            else:
                # Check first entry to get position of FREQ if it exists
                if VAF_check:
                    tokens = line.strip("\n").split("\t")
                    format_field = tokens[8].split(":")
                    for i in range(0,len(format_field)):
                        if format_field[i] == "FREQ":
                            VAF_pos = i
                            break
                # Return None if VCF does not contain VAF data
                else:
                    VAF_pos = None
                    break
    return VAF_pos

def custom_bisect_left(a, x, lo=0, hi=None, getter=0):
    """ Same as bisect.bisect_left, but compares only index "getter"

        See bisect_left source for more info.
    """

    if lo < 0:
        raise ValueError('lo must be non-negative')
    if hi is None:
        hi = len(a)
    while lo < hi:
        mid = (lo+hi)//2
        if a[mid][getter] < x: lo = mid+1
        else: hi = mid
    return lo

def seq_to_peptide(seq, reverse_strand=False):
    """ Translates nucleotide sequence into peptide sequence.

        All codons including and after stop codon are recorded as X's.

        seq: nucleotide sequence
        reverse_strand: True iff strand is -

        Return value: peptide string
    """
    seq_size = len(seq)
    if reverse_strand:
        seq = seq[::-1].translate(_complement_table)
    peptide = []
    for i in xrange(0, seq_size - seq_size % 3, 3):
        codon = _codon_table[seq[i:i+3]]
        peptide.append(codon)
        if codon == 'X':
            break
    for j in xrange(i + 3, seq_size - seq_size % 3, 3):
        peptide.append('X')
    return ''.join(peptide)

def median(numbers):
    numbers = sorted(numbers)
    center = len(numbers) / 2
    if len(numbers) == 1:
        return numbers[0]
    elif len(numbers) % 2 == 0:
        return sum(numbers[center - 1:center + 1]) / 2.0
    else:
        return numbers[center]

def kmerize_peptide(peptide, min_size=8, max_size=11):
    """ Obtains subsequences of a peptide.

        normal_peptide: normal peptide seq
        min_size: minimum subsequence size
        max_size: maximum subsequence size

        Return value: list of all possible subsequences of size between
            min_size and max_size
    """
    peptide_size = len(peptide)
    return [item for sublist in
                [[peptide[i:i+size] for i in xrange(peptide_size - size + 1)]
                    for size in xrange(min_size, max_size + 1)]
            for item in sublist if 'X' not in item]

def neoepitopes(mutated_seq, reverse_strand=False, min_size=8, max_size=11):
    """ Finds neoepitopes from normal and mutated seqs.

        mutated_seq: mutated nucelotide sequence
        reverse_strand: True iff strand is -
        min_size: minimum peptide kmer size to write
        max_size: maximum petide kmer size to write

        Return value: List of mutated_kmers
    """
    return kmerize_peptide(seq_to_peptide(mutated_seq, 
                                            reverse_strand=reverse_strand
                                            ),
                            min_size=min_size,
                            max_size=max_size
                            )

def process_haplotypes(hapcut_output, interval_dict):
    """ Stores all haplotypes relevant to different transcripts as a dictionary

        hapcut_output: output from HAPCUT2, adjusted to include unphased 
                        mutations as their own haplotypes (performed in 
                        software's prep mode)
        interval_dict: dictionary linking genomic intervals to transcripts

        Return value: dictinoary linking haplotypes to transcripts
    """
    affected_transcripts = {}
    with open(hapcut_output, "r") as f:
        block_transcripts = {}
        for line in f:
            if line.startswith('BLOCK'):
                # Skip block header lines
                continue
            elif line[0] == "*":
                # Process all transcripts for the block
                for transcript_ID in block_transcripts:
                    block_transcripts[transcript_ID].sort(key=itemgetter(1))
                    if transcript_ID not in affected_transcripts:
                        affected_transcripts[transcript_ID] = [
                                            block_transcripts[transcript_ID]
                                            ]
                    else:
                        affected_transcripts[transcript_ID].append(
                                                block_transcripts[transcript_ID]
                                                )
                # Reset transcript dictionary
                block_transcripts = {}
            else:
                # Add mutation to transcript dictionary for the block
                tokens = line.strip("\n").split("\t")
                mut_size = min(len(tokens[5]), len(tokens[6]))
                end = tokens[4] + mut_size
                overlapping_transcripts = get_transcripts_from_tree(
                                                                  tokens[3], 
                                                                  tokens[4], 
                                                                  end,
                                                                  interval_dict
                                                                   )
                # For each overlapping transcript, add mutation entry
                # Contains chromosome, position, reference, alternate, allele
                #   A, allele B, genotype line from VCF
                for transcript in overlapping_transcripts:
                    if transcript not in block_transcripts:
                        block_transcripts[transcript] = [[tokens[3], tokens[4], 
                                                          tokens[5], tokens[6], 
                                                          tokens[1], tokens[2], 
                                                          tokens[7]]]
                    else:
                        block_transcripts[transcript].append([tokens[3], 
                                                              tokens[4], 
                                                              tokens[5], 
                                                              tokens[6], 
                                                              tokens[1], 
                                                              tokens[2], 
                                                              tokens[7]])
    return affected_transcripts


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=_help_intro, 
                                        formatter_class=help_formatter)
    subparsers = parser.add_subparsers(help=(
                                    'subcommands; add "-h" or "--help" '
                                    'after a subcommand for its parameters'
                                ), dest='subparser_name')
    test_parser = subparsers.add_parser('test', help='performs unit tests')
    index_parser = subparsers.add_parser('index',
                                        help=('produces pickled dictionaries '
                                        'linking transcripts to intervals and '
                                        ' CDS lines in a GTF'))
    merge_parser = subparsers.add_parser('merge',
                                         help=('merges germline and somatic '
                                               'VCFS for combined mutation '
                                               'phasing with HAPCUT2'))
    prep_parser = subparsers.add_parser('prep',
                                        help=('combines HAPCUT2 output with '
                                              'unphased variants for call '
                                              'mode'))
    call_parser = subparsers.add_parser('call', help='calls neoepitopes')
    index_parser.add_argument('-g', '--gtf', type=str, required=True,
            help='input path to GTF file'
        )  
    index_parser.add_argument('-d', '--dicts', type=str, required=True,
            help='output path to pickled CDS dictionary'
        )
    merge_parser.add_argument('-g', '--germline', type=str, required=True,
            help='input path to germline VCF'
        )
    merge_parser.add_argument('-s', '--somatic', type=str, required=True,
            help='input path to somatic VCF'
        )
    merge_parser.add_argument('-o', '--output', type=str, required=False,
            help='output path to combined VCF'
        )
    prep_parser.add_argument('-v', '--vcf', type=str, required=True,
            help='input VCF'
        )
    prep_parser.add_argument('-c', '--hapcut2-output', type=str, required=True,
            help='path to output file of HAPCUT2 run on input VCF'
        )
    prep_parser.add_argument('-o', '--output', type=str, required=True,
            help='path to output file to be input to call mode'
        )
    call_parser.add_argument('-x', '--bowtie-index', type=str, required=True,
            help='path to Bowtie index basename'
        )
    call_parser.add_argument('-v', '--vcf', type=str, required=True,
            help='input path to VCF'
        )
    call_parser.add_argument('-d', '--dicts', type=str, required=True,
            help='input path to pickled CDS dictionary'
        )
    call_parser.add_argument('-c', '--merged-hapcut2-output', type=str,
            required=True,
            help='path to output of prep subcommand'
        )
    call_parser.add_argument('-k', '--kmer-size', type=str, required=False,
            default='8,11', help='kmer size for epitope calculation'
        )
    call_parser.add_argument('-m', '--method', type=str, required=False,
            default='-', 
            help='method for calculating epitope binding affinities'
        )
    call_parser.add_argument('-p', '--affinity-predictor', type=str, 
            required=False, default='netMHCpan', 
            help='path to executable for binding affinity prediction software'
        )
    call_parser.add_argument('-a', '--allele', type=str, required=True,
            help='allele; see documentation online for more information'
        )
    call_parser.add_argument('-f', '--VAF-freq-calc', type=str, required=False,
            default='median',
            help='method for calculating VAF: choice of mean, median, min, max'
        )
    args = parser.parse_args()
    
    if args.subparser_name == 'test':
        del sys.argv[1:] # Don't choke on extra command-line parameters
        import unittest
        import filecmp
        class TestGTFprocessing(unittest.TestCase):
            """Tests proper creation of dictionaries store GTF data"""
            def setUp(self):
                """Sets up gtf file and creates dictionaries for tests"""
                self.gtf = os.path.join(
                                    os.path.dirname(
                                            os.path.dirname(
                                                    os.path.realpath(__file__)
                                                )
                                        ), 'test', 'Ychrom.gtf'
                                )
                self.Ycds = gtf_to_cds(self.gtf, "NA", pickle_it=False)
                self.Ytree = cds_to_tree(self.Ycds, "NA", pickle_it=False)
            def test_transcript_to_CDS(self):
                """Fails if dictionary was built incorrectly"""
                self.assertEqual(len(self.Ycds.keys()), 220)
            def test_CDS_tree(self):
                """Fails if dictionary was built incorrectly"""
                self.assertEqual(len(self.Ytree.keys()), 1)
                self.assertEqual(len(self.Ytree["Y"]), 2138)
            def test_transcript_extraction(self):
                """Fails if incorrect transcripts are pulled"""
                self.assertEqual(len(get_transcripts_from_tree(
                                                                "Y", 150860, 
                                                                150861,
                                                                self.Ytree)), 
                                                                10
                                                                )
                self.coordinate_search = list(self.Ytree["Y"].search(150860,
                                                                     150861))
                self.transcripts = []
                for interval in self.coordinate_search:
                    self.transcripts.append(interval[2])
                self.transcripts.sort()
                self.assertEqual(self.transcripts, [
                                                    'ENST00000381657.7_3_PAR_Y', 
                                                    'ENST00000381663.8_3_PAR_Y', 
                                                    'ENST00000399012.6_3_PAR_Y', 
                                                    'ENST00000415337.6_3_PAR_Y', 
                                                    'ENST00000429181.6_2_PAR_Y', 
                                                    'ENST00000430923.7_3_PAR_Y', 
                                                    'ENST00000443019.6_2_PAR_Y', 
                                                    'ENST00000445062.6_2_PAR_Y', 
                                                    'ENST00000447472.6_3_PAR_Y', 
                                                    'ENST00000448477.6_2_PAR_Y'
                                                    ])
        class TestVCFmerging(unittest.TestCase):
            """Tests proper merging of somatic and germline VCFS"""
            def setUp(self):
                """Sets up files to use for tests"""
                self.varscan = os.path.join(
                                    os.path.dirname(
                                            os.path.dirname(
                                                    os.path.realpath(__file__)
                                                )
                                        ), 'test', 'Ychrom.varscan.vcf'
                                )                
                self.germline = os.path.join(
                                    os.path.dirname(
                                            os.path.dirname(
                                                    os.path.realpath(__file__)
                                                )
                                        ), 'test', 'Ychrom.germline.vcf'
                                )   
                self.precombined = os.path.join(
                                    os.path.dirname(
                                            os.path.dirname(
                                                    os.path.realpath(__file__)
                                                )
                                        ), 'test', 'Ychrom.combined.vcf'
                                )  
                self.outvcf = os.path.join(
                                    os.path.dirname(
                                            os.path.dirname(
                                                    os.path.realpath(__file__)
                                                )
                                        ), 'test', 'Ychrom.testcombine.vcf'
                                )  
                combinevcf(self.germline, self.varscan, self.outvcf)
            def test_merge(self):
                """Fails if VCFs were merged improperly"""
                self.assertTrue(filecmp.cmp(self.outvcf, self.precombined))
            def tearDown(self):
                """Removes test file"""
                os.remove(self.outvcf)
        class TestVAFpos(unittest.TestCase):
            """Tests fetching of VAF position from VCF file"""
            def setUp(self):
                """ Sets up vcf files to use for tests """
                self.varscan = os.path.join(
                                    os.path.dirname(
                                            os.path.dirname(
                                                    os.path.realpath(__file__)
                                                )
                                        ), 'test', 'Ychrom.varscan.vcf'
                                )
                self.mutect = os.path.join(
                                    os.path.dirname(
                                            os.path.dirname(
                                                    os.path.realpath(__file__)
                                                )
                                        ), 'test', 'Ychrom.mutect.vcf'
                                )
            def test_position(self):
                """Fails if incorrect positions are returned"""
                self.assertEqual(get_VAF_pos(self.varscan), 5)
                self.assertEqual(get_VAF_pos(self.mutect), None)
        class TestTranscript(unittest.TestCase):
            """Tests transcript object construction"""
            def setUp(self):
                """Sets up files for testing"""
                self.fasta = os.path.join(
                                    os.path.dirname(
                                            os.path.dirname(
                                                    os.path.realpath(__file__)
                                                )
                                        ), 'test', 'ref.fasta'
                                )
                self.ref_prefix = os.path.join(
                                    os.path.dirname(
                                            os.path.dirname(
                                                    os.path.realpath(__file__)
                                                )
                                        ), 'test', 'ref'
                                )
                with open(self.fasta, "w") as f:
                    f.write(">1 dna:chromosome\n")
                    f.write("ACGCCCGTGACTTATTCGTGTGCAGACTAC\n")
                    f.write("ATGCCCGTGCCGAATTCGTGTCCCCGCTAC\n")
                    f.write("AATGCCCGTGCCGATTTGAAACCCCGCTAC\n")
                subprocess.call(["bowtie-build", self.fasta, self.ref_prefix])
                self.reference_index = bowtie_index.BowtieIndexReference(
                                                                self.ref_prefix)
                self.CDS = ["1", 'blah', 'blah', "31", "75", '.', "+"]
                self.stop = ["1", 'blah', 'blah', "76", "78", '.', "+"]
                self.transcript = Transcript(self.reference_index, 
                                                [self.CDS, self.stop])
            def test_irrelevant_edit(self):
                """Fails if edit is made for non-CDS/stop position"""
                self.transcript.edit("G", 1)
                relevant_edits = self.transcript.expressed_edits()
                self.assertEqual(self.transcript.edits[0], [("G", "V", "S")])
                self.assertEqual(relevant_edits[0], {})
                self.assertEqual(relevant_edits[1], [(29, "R"), (74, "R"), 
                                                        (74, "R"), (77, "R")])
            def test_relevant_edit(self):
                """Fails if edit is not made for CDS position"""
                self.transcript.edit("G", 34)
                relevant_edits = self.transcript.expressed_edits()
                self.assertEqual(self.transcript.edits[33], [("G", "V", "S")])
                self.assertEqual(relevant_edits[0][33], [("G", "V", "S")])
                self.assertEqual(relevant_edits[1], [(29, "R"), (74, "R"), 
                                                        (74, "R"), (77, "R")])
            def test_reset_to_reference(self):
                """Fails if transcript is not reset to reference"""
                self.transcript.edit("G", 34)
                self.transcript.reset(reference=True)
                self.assertEqual(self.transcript.edits, {})
            def test_edit_and_save(self):
                """Fails if edits aren't saved"""
                self.transcript.edit("G", 34)
                self.transcript.edit("CCC", 60, mutation_type="D")
                self.transcript.save()
                self.assertEqual(self.transcript.last_edits[33], [("G", "V", 
                                                                    "S")])
                self.assertEqual(self.transcript.last_deletion_intervals,
                                    [(58, 61, "S")])
            def test_reset_to_save_point(self):
                """Fails if new edit not erased or old edits not retained"""
                self.transcript.edit("G", 34)
                self.transcript.edit("CCC", 60, mutation_type="D")
                self.transcript.save()
                self.transcript.edit("C", 36)
                self.assertEqual(self.transcript.edits[35], [("C", "V", "S")])
                self.transcript.reset(reference=False)
                self.assertNotIn(35, self.transcript.edits)
                self.assertEqual(self.transcript.last_edits[33], [("G", "V", 
                                                                    "S")])
                self.assertEqual(self.transcript.last_deletion_intervals,
                                    [(58, 61, "S")])
                self.assertNotEqual(self.transcript.edits, {})
            def test_SNV_seq(self):
                """Fails if SNV is edited incorrectly"""
                self.transcript.edit("G", 31)
                seq1 = self.transcript.annotated_seq()
                seq2 = self.transcript.annotated_seq(31, 36)
                self.assertEqual(seq1, [('G', 'S'), 
                    ('TGCCCGTGCCGAATTCGTGTCCCCGCTACAATGCCCGTGCCGATTTG', 'R')])
                self.assertEqual(seq2, [('G', 'S'), ('TGCCC', 'R')])
            def test_inside_indel(self):
                """Fails if indel within CDS is inserted incorrectly"""
                self.transcript.edit("Q", 35, mutation_type="I")
                self.assertEqual(self.transcript.edits[34], [("Q", "I", "S")])
                seq1 = self.transcript.annotated_seq()
                seq2 = self.transcript.annotated_seq(31, 36)
                self.assertEqual(seq1, [('ATGCC', 'R'), ('Q', 'S'), 
                        ('CGTGCCGAATTCGTGTCCCCGCTACAATGCCCGTGCCGATTTG', 'R')])
                self.assertEqual(seq2, [('ATGCC', 'R'), ('Q', 'S'), ('C', 'R')])
            def test_adjacent_indel(self):
                """Fails if indel right before CDS is inserted incorrectly"""
                self.transcript.edit("Q", 30, mutation_type="I")
                self.assertEqual(self.transcript.edits[29], [("Q", "I", "S")])
                seq1 = self.transcript.annotated_seq()
                seq2 = self.transcript.annotated_seq(30, 36)
                ## DEBUG THESE
                self.assertEqual(seq1, [('Q', 'S'), 
                    ('ATGCCCGTGCCGAATTCGTGTCCCCGCTACAATGCCCGTGCCGATTTG', 'R')])
                self.assertEqual(seq2, [('Q', 'S'), ('ATGCCC', 'R')])
            def compound_variants(self):
                self.transcript.edit("Q", 30, mutation_type ="I")
                self.transcript.edit("T", 33, mutation_type ="V")
                self.transcript.edit("JJJ", 40, mutation_type ="I")
                self.transcript.edit(1, 45, mutation_type ="D")
                seq1 = self.transcript.annotated_seq()
                seq2 = self.transcript.annotated_seq(30, 36)
                self.assertEqual(seq1, [('Q', 'S'), ('AT', 'R'), ('T', 'S'), 
                                        ('CCCGTGC', 'R'),  ('JJJ', 'S'), 
                                        ('CGAA', 'R'), ('', 'S'), 
                                        ('TCGTGTCCCCGCTACAATGCCCGTGCCGATTTG', 
                                            'R')])
                self.assertEqual(seq2, [('Q', 'S'), ('AT', 'R'), ('T', 'S'), 
                                                                ('CC', 'R')])
            def test_deletion(self):
                """Fails if deletion is made incorrectly"""
                self.transcript.edit(5, 34, mutation_type="D")
                self.assertEqual(self.transcript.deletion_intervals, [(32, 37, 
                                                                        "S")])
                seq1 = self.transcript.annotated_seq()
                seq2 = self.transcript.annotated_seq(31, 36)
                self.assertEqual(seq1, [('ATG', 'R'), ('', 'S'), 
                            ('GCCGAATTCGTGTCCCCGCTACAATGCCCGTGCCGATTTG', 'R')])
                ### DEBUG THIS ONE
                self.assertEqual(seq2, [('ATG', 'R'), ('', 'S'), ('GCC', 'R')])
            def tearDown(self):
                """Removes temporary files"""
                ref_remove = os.path.join(
                                    os.path.dirname(
                                            os.path.dirname(
                                                    os.path.realpath(__file__)
                                                )
                                        ), 'test', 'ref*ebwt'
                                )
                subprocess.call(["rm", ref_remove])
                subprocess.call(["rm", self.fasta])
        class TestHAPCUT2Processing(unittest.TestCase):
            """Tests proper processing of HAPCUT2 files"""
            def setUp(self):
                """Sets up input files and dictionaries to use for tests"""
                self.gtf = os.path.join(
                                    os.path.dirname(
                                            os.path.dirname(
                                                    os.path.realpath(__file__)
                                                )
                                        ), 'test', 'Ychrom.gtf'
                                )
                self.Ycds = gtf_to_cds(self.gtf, "NA", pickle_it=False)
                self.Ytree = cds_to_tree(self.Ycds, "NA", pickle_it=False)
                self.Yhapcut = os.path.join(
                                    os.path.dirname(
                                            os.path.dirname(
                                                    os.path.realpath(__file__)
                                                )
                                        ), 'test', 'Ychrom.hap.out'
                                )         
            def test_hap_processing(self):
                '''
                """Fails if file is processed incorrectly"""
                Ynorm, Ytum, YVAF = process_haplotypes(self.Yhapcut, self.Ycds, 
                                                    self.Ytree, None, [8,11])
                self.assertEqual([len(Ynorm), len(Ytum), len(YVAF)], [0,0,0])
                #### WRITE TEST FOR CASE WHERE THERE WILL BE EPITOPES ####
                '''
        unittest.main()
    elif args.subparser_name == 'index':
        cds_dict = gtf_to_cds(args.gtf, args.dicts)
        tree = cds_to_tree(cds_dict, args.dicts)
        # FM indexing of proteome??
    elif args.subparser_name == 'swap':
        adjust_tumor_column(args.input, args.output)
    elif args.subparser_name == 'merge':
        combinevcf(args.germline, args.somatic, outfile=args.output)
    elif args.subparser_name == 'prep':
        phased = collections.defaultdict(set)
        with open(args.output, 'w') as output_stream:
            print >>output_stream, '********'
            with open(args.hapcut2_output) as hapcut2_stream:
                for line in hapcut2_stream:
                    if line[0] != '*' and not line.startswith('BLOCK'):
                        tokens = line.strip().split('\t')
                        phased[(tokens[3], int(tokens[4]))].add(
                                                        (tokens[5], tokens[6])
                                                    )
                    print >>output_stream, line,
            with open(args.vcf) as vcf_stream:
                first_char = '#'
                while first_char == '#':
                    line = vcf_stream.readline().strip()
                    try:
                        first_char = line[0]
                    except IndexError:
                        first_char = '#'
                counter = 1
                while line:
                    tokens = line.split('\t')
                    pos = int(tokens[1])
                    if (tokens[3], tokens[4]) not in phased[
                                                (tokens[0], pos)
                                            ]:
                        print >>output_stream, 'BLOCK: unphased'
                        print >>output_stream, ('{vcf_line}\tNA\tNA\t{chrom}\t'
                                               '{pos}\t{ref}\t{alt}\t'
                                               '{genotype}\tNA\tNA\tNA').format(
                                                    vcf_line=counter,
                                                    chrom=tokens[0],
                                                    pos=pos,
                                                    ref=tokens[3],
                                                    alt=tokens[4],
                                                    genotype=tokens[9]
                                                )
                        print >>output_stream, '********' 
                    line = vcf_stream.readline().strip()
                    counter += 1
    elif args.subparser_name == 'call':
        # Load pickled dictionaries
        interval_dict = pickle.load(open(args.dicts + "".join([dictdir, 
                                "/", "intervals_to_transcript.pickle"]), "rb"))
        cds_dict = pickle.load(open(args.dicts + "".join([dictdir, 
                                "/", "transcript_to_CDS.pickle"]), "rb"))
        # Check affinity predictor
        program = which(args.affinity_predictor)
        if program is None:
            raise ValueError(" ".join([program, "is not a valid software"]))
        elif "netMHCIIpan" in program:
            def get_affinity(peptides, allele, netmhciipan=program,
                                            remove_files=True):
                """ Obtains binding affinities from list of peptides

                    peptides: peptides of interest (list of strings)
                    allele: Allele to use for binding affinity (string)
                    remove_files: option to remove intermediate files

                    Return value: affinities (a list of binding affinities 
                                    as strings)
                """
                files_to_remove = []
                try:
                    # Check that allele is valid for method
                    with open("".join([os.path.dirname(__file__),
                             "/availableAlleles.pickle"]), "rb"
                            ) as allele_stream:
                        avail_alleles = pickle.load(allele_stream)
                    # Homogenize format
                    allele = allele.replace("HLA-", "")
                    if allele not in avail_alleles["netMHCIIpan"]:
                        sys.exit(" ".join([allele,
                                 " is not a valid allele for netMHCIIpan"])
                                )
                    # Establish return list and sample id
                    sample_id = '.'.join([peptides[0],
                                            str(len(peptides)), allele,
                                            'netmhciipan'])
                    affinities = []
                    # Write one peptide per line to a temporary file for 
                    #   input if peptide length is at least 9
                    # Count instances of smaller peptides
                    na_count = 0
                    peptide_file = tempfile.mkstemp(
                                    suffix=".peptides", prefix="id.", text=True)
                    files_to_remove.append(peptide_file)
                    with open(peptide_file[1], "w") as f:
                        for sequence in peptides:
                            if len(sequence) >= 9:
                                print >>f, sequence
                            else:
                                na_count += 1
                    if na_count > 0:
                        print ' ' .join(['Warning:', str(na_count),
                                        'peptides not compatible with'
                                        'netMHCIIpan will not receive score'])
                    # Establish temporary file to hold output
                    mhc_out = tempfile.mkstemp(suffix=".netMHCIIpan.out", 
                                                prefix="id.", text=True)
                    files_to_remove.append(mhc_out)
                    # Run netMHCIIpan
                    subprocess.check_call(
                                    [netmhciipan, "-a", allele, "-inptype", "1", 
                                     "-xls", "-xlsfile", mhc_out, peptide_file]
                                )
                    # Retrieve scores for valid peptides
                    score_dict = {}
                    with open(mhc_out[1], "r") as f:
                        # Skip headers
                        f.readline()
                        f.readline()
                        for line in f:
                            tokens = line.split('\t')
                            # token 1 is peptide; token 4 is score
                            score_dict[tokens[1]] = tokens[4]
                    # Produce list of scores for valid peptides
                    # Invalid peptides receive "NA" score
                    for sequence in peptides:
                        if sequence in score_dict:
                            nM = score_dict[sequence]
                        else:
                            nM = "NA"
                            affinities.append(nM)
                    return affinities
                finally:
                    if remove_files:
                        for file_to_remove in files_to_remove:
                            os.remove(file_to_remove)
        elif "netMHCpan" in program:
            # define different affinity prediction function here
            def get_affinity(peptides, allele, netmhcpan=program,
                                remove_files=True):
                """ Obtains binding affinities from list of peptides

                    peptides: peptides of interest (list of strings)
                    allele: allele to use for binding affinity 
                                (string, format HLA-A02:01)
                    remove_files: option to remove intermediate files

                    Return value: affinities (a list of binding affinities 
                                    as strings)
                """
                files_to_remove = []
                try:
                    # Check that allele is valid for method
                    with open("".join([os.path.dirname(__file__),
                             "/availableAlleles.pickle"]), "rb"
                            ) as allele_stream:
                        avail_alleles = pickle.load(allele_stream)
                    allele = allele.replace("*", "")
                    if allele not in avail_alleles["netMHCpan"]:
                        sys.exit(" ".join([allele,
                                 " is not a valid allele for netMHC"])
                                )
                    # Establish return list and sample id
                    sample_id = '.'.join([peptides[0], str(len(peptides)), 
                                            allele, 'netmhcpan'])
                    affinities = []

                    # Write one peptide per line to a temporary file for input
                    peptide_file = tempfile.mkstemp(suffix=".peptides", 
                                                    prefix="".join([sample_id, 
                                                                    "."]), 
                                                    text=True)
                    files_to_remove.append(peptide_file)
                    with open(peptide_file[1], "w") as f:
                        for sequence in peptides:
                            print >>f, sequence

                    # Establish temporary file to hold output
                    mhc_out = tempfile.mkstemp(suffix=".netMHCpan.out", 
                                                prefix="".join([sample_id, 
                                                                "."]), 
                                                text=True)
                    files_to_remove.append(mhc_out)
                    # Run netMHCpan
                    subprocess.check_call(
                        [netmhcpan, "-a", allele, "-inptype", "1", "-p", "-xls", 
                            "-xlsfile", mhc_out, peptide_file])
                    with open(mhc_out[1], "r") as f:
                        f.readline()
                        f.readline()
                        for line in f:
                            line = line.strip("\n").split("\t")
                            nM = line[5]
                            affinities.append(nM)
                    return affinities
                finally:
                    # Remove temporary files
                    if remove_files:
                        for file_to_remove in files_to_remove:
                            os.remove(file_to_remove)
        else:
            raise ValueError(" ".join([program, "is not a valid software"]))
        # Obtain VAF frequency VCF position
        VAF_pos = get_VAF_pos(args.vcf)
        # Obtain peptide sizes for kmerizing peptides
        if ',' in args.kmer_size:
            size_list = args.kmer_size.split(',')
            size_list.sort()
            for i in range(0, len(size_list)):
                size_list[i] = int(size_list[i])
        # For retrieving genome sequence
        reference_index = bowtie_index.BowtieIndexReference(args.bowtie_index)
        # Find transcripts that haplotypes overlap 
        relevant_transcripts = process_haplotypes(args.merged_hapcut2_output, 
                                                    interval_dict)
        # Iterate over relevant transcripts to create transcript objects and
        #   enumerate neoepitopes
        for affected_transcript in relevant_transcripts:
            # Create transcript object
            transcript = Transcript(reference_index, 
                            [[str(chrom), 'blah', 'blah', str(start), str(end), 
                              '.', strand] for (chrom, start, end,  strand) in 
                              cds_dict[transcript_ID]]
                            )
            # Iterate over haplotypes associated with this transcript
            haplotypes = relevant_transcripts[affected_transcript]
            for ht in haplotypes:
                # Make edits for each mutation
                for mutation in ht:
                    # Determine type of mutation
                    if len(mutation[5] == len(mutation[6])):
                        mutation_type = 'V'
                    elif len(mutation[5]) < len(mutation[6]):
                        mutation_type = 'I'
                    elif len(mutation[5]) > len(mutation[6]):
                        mutation_type = 'D'
                    else:
                        mutation_type = '?'
                    # Determine if mutation is somatic or germline
                    if mutation[7][-1] == "*":
                        mutation_class = 'G'
                    else:
                        mutation_class = 'S'
                    # Make edit to transcript
                    transcipt.edit(mutation[3], mutation[1], 
                                    mutation_type=mutation_type, 
                                    mutation_class=mutation_class)
                ## ENUMERATE NEOEPITOPES HERE
                transcript.reset(reference=True)
    else:
        sys.exit("".join([args.subparser_name, 
                            " is not a valid software mode"]))

