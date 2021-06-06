import argparse
from pkg_resources import resource_filename
from pangolin.model import *
import vcf
import pybedtools
import gffutils
import pandas as pd
from pyfaidx import Fasta
# import time
# startTime = time.time()

IN_MAP = np.asarray([[0, 0, 0, 0],
                     [1, 0, 0, 0],
                     [0, 1, 0, 0],
                     [0, 0, 1, 0],
                     [0, 0, 0, 1]])


def one_hot_encode(seq, strand):
    seq = seq.upper().replace('A', '1').replace('C', '2')
    seq = seq.replace('G', '3').replace('T', '4').replace('N', '0')
    if strand == '+':
        seq = np.asarray(list(map(int, list(seq))))
    elif strand == '-':
        seq = np.asarray(list(map(int, list(seq[::-1]))))
        seq = (5 - seq) % 5  # Reverse complement
    return IN_MAP[seq.astype('int8')]


def compute_score(ref_seq, alt_seq, strand, d, models):
    ref_seq = one_hot_encode(ref_seq, strand).T
    ref_seq = torch.from_numpy(np.expand_dims(ref_seq, axis=0)).float()
    alt_seq = one_hot_encode(alt_seq, strand).T
    alt_seq = torch.from_numpy(np.expand_dims(alt_seq, axis=0)).float()

    if torch.cuda.is_available():
        ref_seq = ref_seq.to(torch.device("cuda"))
        alt_seq = alt_seq.to(torch.device("cuda"))

    pangolin = []
    for j in range(1):
        score = []
        for model in models[5*j:5*j+1]:
            with torch.no_grad():
                ref = model(ref_seq)[0][[1,4,10][j],:].cpu().numpy()
                alt = model(alt_seq)[0][[1,4,10][j],:].cpu().numpy()
                if strand == '-':
                    ref = ref[::-1]
                    alt = alt[::-1]
                if len(ref)>len(alt):
                    alt = np.concatenate([alt[0:d+1],np.zeros(len(ref)-len(alt)),alt[-d:]])
                elif len(ref)<len(alt):
                    ref = np.concatenate([ref[0:d+1],np.zeros(len(alt)-len(ref)),ref[-d:]])
                score.append(alt-ref)
        pangolin.append(np.mean(score, axis=0))

    pangolin = np.array(pangolin)
    loss = pangolin[np.argmin(pangolin, axis=0), np.arange(pangolin.shape[1])]
    gain = pangolin[np.argmax(pangolin, axis=0), np.arange(pangolin.shape[1])]
    return loss, gain


def get_genes(chr, pos, gtf):
    genes = gtf.region((chr, pos-1, pos-1), featuretype="gene")
    genes_pos, genes_neg = {}, {}

    for gene in genes:
        if gene[3] > pos or gene[4] < pos:
            continue
        gene = gene["gene_id"][0]
        exons = []
        for exon in gtf.children(gene, featuretype="exon"):
            exons.extend([exon[3], exon[4]])
        if exon[6] == '+':
            genes_pos[gene] = exons
        elif exon[6] == '-':
            genes_neg[gene] = exons

    return (genes_pos, genes_neg)


def process_variant(lnum, chr, pos, ref, alt, gtf, models, args):
    d = args.distance
    cutoff = args.score_cutoff

    if len(set("ACGT").intersection(set(ref))) == 0 or len(set("ACGT").intersection(set(alt))) == 0 \
            or (len(ref) != 1 and len(alt) != 1):
        print("[Line %s]" % lnum, "WARNING, skipping variant: Variant format not supported.")
        return -1
    elif len(ref) > 2*d:
        print("[Line %s]" % lnum, "WARNING, skipping variant: Deletion too large")
        return -1

    index = Fasta(args.reference_file)
    # try to make vcf chromosomes compatible with reference chromosomes
    if chr not in index.keys() and "chr"+chr in index.keys():
        chr = "chr"+chr
    elif chr not in index.keys() and chr[3:] in index.keys():
        chr = chr[3:]

    bed = pybedtools.BedTool("""%s %s %s""" % (chr, pos-5001-d, pos+len(ref)+4999+d), from_string=True)
    try:
        seq = bed.sequence(fi=args.reference_file)
        seq = open(seq.seqfn).read().split('\n')[1]
    except Exception as e:
        print(e)
        print("[Line %s]" % lnum, "WARNING, skipping variant: See error message above.")
        return -1

    if seq[5000+d:5000+d+len(ref)] != ref:
        print("[Line %s]" % lnum, "WARNING, skipping variant: Mismatch between FASTA (ref base: %s) and variant file (ref base: %s)."
              % (seq[5000+d:5000+d+len(ref)], ref))
        return -1

    ref_seq = seq
    alt_seq = seq[:5000+d] + alt + seq[5000+d+len(ref):]

    # get genes that intersect variant
    genes_pos, genes_neg = get_genes(chr, pos, gtf)
    if len(genes_pos)+len(genes_neg)==0:
        print("[Line %s]" % lnum, "WARNING, skipping variant: Variant not contained in a gene body. Do GTF/FASTA chromosome names match?")
        return -1

    # get splice scores
    loss_pos, gain_pos = None, None
    if len(genes_pos) > 0:
        loss_pos, gain_pos = compute_score(ref_seq, alt_seq, '+', d, models)
    loss_neg, gain_neg = None, None
    if len(genes_neg) > 0:
        loss_neg, gain_neg = compute_score(ref_seq, alt_seq, '-', d, models)

    scores = ""
    for (genes, loss, gain) in \
            ((genes_pos,loss_pos,gain_pos),(genes_neg,loss_neg,gain_neg)):
        for gene, positions in genes.items():
            positions = np.array(positions)
            if args.mask == "True":
                positions = positions-(pos-d)
                if len(alt_seq)>len(ref_seq):
                    positions[positions>d] += (len(alt_seq)-len(ref_seq))
                positions = positions[(positions>=0) & (positions<len(loss))]
                # set splice gain at annotated sites to 0
                gain[positions] = np.minimum(gain[positions], 0)
                # set splice loss at unannotated sites to 0
                loss[-positions] = np.maximum(loss[-positions], 0)

            if cutoff != None:
                l, g = np.where(loss<=-cutoff)[0], np.where(gain>=cutoff)[0]
                scores = "|".join([str(round(_,2)) for _ in np.concatenate([gain[g],loss[l]])]
                                  +[str(_) for _ in np.concatenate([g-d,l-d])])
            else:
                l, g = np.argmin(loss), np.argmax(gain),
                scores = scores+"%s|%s|%s|%s|%s|" % (gene, round(gain[g],2), round(loss[l],2), g-d, l-d)

    return scores.strip('|')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("variant_file", help="VCF or CSV file with a header (see COLUMN_IDS option).")
    parser.add_argument("reference_file", help="FASTA file containing a reference genome sequence.")
    parser.add_argument("annotation_file", help="gffutils database file. Can be generated using create_db.py.")
    parser.add_argument("output_file", help="Prefix for output file. Will be a VCF/CSV if variant_file is VCF/CSV.")
    parser.add_argument("-c", "--column_ids", default="CHROM,POS,REF,ALT", help="(If variant_file is a CSV) Column IDs for: chromosome, variant position, reference bases, and alternative bases. "
                                                                                "Separate IDs by commas. (Default: CHROM,POS,REF,ALT)")
    parser.add_argument("-m", "--mask", default="False", choices=["False","True"], help="If True, splice gains at annotated splice sites and splice losses at unannotated splice sites will be set to 0.")
    parser.add_argument("-s", "--score_cutoff", type=float, help="Output all sites with absolute predicted change in score >=cutoff, instead of only the maximum loss/gain sites.")
    parser.add_argument("-d", "--distance", type=int, default=50, help="Number of bases on either side of the variant for which splice scores should be calculated. (Default: 50)")
    args = parser.parse_args()

    variants = args.variant_file
    gtf = args.annotation_file
    try:
        gtf = gffutils.FeatureDB(gtf)
    except:
        print("ERROR, annotation_file could not be opened. Is it a gffutils database file?")
        exit()

    models = []
    for i in [0,2,6]:
        for j in range(1,6):
            model = Pangolin(L, W, AR)
            if torch.cuda.is_available():
                model.cuda()
                weights = torch.load(resource_filename(__name__,"models/final.%s.%s.3" % (j, i)))
            else:
                weights = torch.load(resource_filename(__name__,"models/final.%s.%s.3" % (j, i)), map_location=torch.device('cpu'))
            model.load_state_dict(weights)
            model.eval()
            models.append(model)

    if variants.endswith(".vcf"):
        lnum = 0
        # count the number of header lines
        for line in open(variants, 'r'):
            lnum += 1
            if line[0] != '#':
                break

        variants = vcf.Reader(filename=variants)
        variants.infos["Pangolin"] = vcf.parser._Info(
            "Pangolin",'.',"String","Pangolin splice scores. "
            "Format: gene|gain_1|...|gain_n|loss_1|...|loss_n|gain_1_pos|...|gain_n_pos|loss_1_pos|...|loss_n_pos",'.','.')
        fout = vcf.Writer(open(args.output_file+".vcf", 'w'), variants)

        for i, variant in enumerate(variants):
            scores = process_variant(lnum+i, str(variant.CHROM), int(variant.POS), variant.REF, str(variant.ALT[0]), gtf, models, args)
            if scores != -1:
                variant.INFO["Pangolin"] = scores
            fout.write_record(variant)
            fout.flush()

        fout.close()

    elif variants.endswith(".csv"):
        col_ids = args.column_ids.split(',')
        variants = pd.read_csv(variants, header=0)#, usecols=col_ids)
        fout = open(args.output_file+".csv", 'w')
        fout.write(','.join(variants.columns)+',Pangolin\n')
        fout.flush()

        for lnum, variant in variants.iterrows():
            chr, pos, ref, alt = variant[col_ids]
            ref, alt = ref.upper(), alt.upper()
            scores = process_variant(lnum+1, str(chr), int(pos), ref, alt, gtf, models, args)
            if scores == -1:
                fout.write(','.join(variant.to_csv(header=False, index=False).split('\n'))+'\n')
            else:
                fout.write(','.join(variant.to_csv(header=False, index=False).split('\n'))+scores+'\n')
            fout.flush()

        fout.close()

    else:
        print("ERROR, variant_file needs to be a CSV or VCF.")

    # executionTime = (time.time() - startTime)
    # print('Execution time in seconds: ' + str(executionTime))

if __name__ == '__main__':
    main()
