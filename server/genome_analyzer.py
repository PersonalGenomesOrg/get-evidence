#!/usr/bin/python
# Filename: server.py

"""
usage: %prog [options]
  --pidfile=PATH: location of pid file
  --stderr=PATH: location of log file
  -h, --host=STRING: the host on which to listen
  -p, --port=NUMBER: the port on which to listen
"""

# Start an XMLRPC server for genome analysis.

import multiprocessing
import re
import os
import subprocess
import sys
import fcntl
import gzip
from optparse import OptionParser
from SimpleXMLRPCServer import SimpleXMLRPCServer
from config import GENETESTS_DATA, GETEV_FLAT
from config import DBSNP_B36_SORTED, DBSNP_B37_SORTED
from config import KNOWNGENE_HG18_SORTED, KNOWNGENE_HG19_SORTED
from config import REFERENCE_GENOME_HG18, REFERENCE_GENOME_HG19
from progresstracker import Logger, ProgressTracker
import get_metadata 
import call_missing 
import gff_twobit_query
import gff_dbsnp_query
import gff_nonsynonymous_filter
import gff_getevidence_map
from conversion import cgivar_to_gff, gff_from_23andme

SCRIPT_DIR = os.path.dirname(sys.argv[0])

def detect_format(f_in):
    """Take str generator input, determine genome file format (GFF or CGIVAR)"""
    try:
        line = f_in.next()
    except StopIteration:
        print "No lines in file?"
        return "UNKNOWN"
    # First check for header lines for clues.
    while re.match('#', line):
        if re.match(r'#TYPE.*VAR-ANNOTATION', line):
            print "CGIVAR detected"
            return "CGIVAR"
        if re.match(r'##gff-version', line):
            print "GFF detected"
            return "GFF"
        if re.match(r'# This data file generated by 23andMe', line):
            print "23andme detected"
            return "23ANDME"
        try:
            line = f_in.next()
        except StopIteration:
            print "Nothing but header?"
            return "UNKNOWN"
    # Look at other lines and decide based on their format.
    for i in range(100):
        data = line.split('\t')
        if len(data) < 7:
            try:
                line = f_in.next()
            except StopIteration:
                print "Less than 100 lines?"
                return "UNKNOWN"
            continue
        is_cgi_like = ( re.match(r'chr', data[3]) and 
                     re.match(r'[0-9]', data[4]) and 
                     re.match(r'[0-9]', data[5]) and
                     (data[6] == "no-call" or data[6] == "ref") )
        is_gff_like = ( re.match(r'[0-9]', data[3]) and
                     re.match(r'[0-9]', data[4]) and
                     data[6] == "+" )
        is_23andme_like = ( re.match(r'[0-9]', data[2]) and
                            re.match(r'[ACGT][ACGT]', data[3]) and
                            re.match(r'rs', data[0]) )

        #print str(cgi_like) + " " + str(gff_like) + " " + str(data)
        if is_cgi_like:
            print "CGIVAR guessed"
            return "CGIVAR"
        elif is_gff_like:
            print "GFF guessed"
            return "GFF"
        elif is_23andme_like:
            print "23ANDME guessed"
            return "23ANDME"
        try:
            line = f_in.next()
        except StopIteration:
            break
    print "Giving up after 100 lines... Can't figure out data format."
    return "UNKNOWN"

def process_source(genome_in):
    """
    Take source, uncompress, sort, and convert to GFF as needed, yield GFF
    """
    # Handle genome compression, get input, and make best guess of format type.
    args = { 'genome_in': genome_in, 'cat_command': 'cat ' + genome_in }
    if re.search(r'\.gz', genome_in):
        args['cat_command'] = 'zcat ' + genome_in
    if re.search(r'\.bz2', genome_in):
        args['cat_command'] = 'bzcat ' + genome_in
    source_input = subprocess.Popen(args['cat_command'], shell=True, 
                                    stdout=subprocess.PIPE)
    in_type = detect_format(source_input.stdout)

    # Reset input and output GFF (convert if necessary).
    source_input.stdout.close()
    source_input = subprocess.Popen(args['cat_command'], shell=True,
                                 stdout=subprocess.PIPE)
    if in_type == "GFF":
        gff_input = source_input.stdout
    elif in_type == "CGIVAR":
        gff_input = cgivar_to_gff.convert(source_input.stdout)
    elif in_type == "23ANDME":
        gff_input = gff_from_23andme.convert(source_input.stdout)
    else:
        print "ERROR: genome file format not recognized"

    # Grab header (don't sort) & genome build. Pipe the rest to UNIX sort.
    header_done = False
    header = []
    sort_cmd = ['sort', '--buffer-size=20%', '--key=1,1', '--key=4n,4']
    sort_out = subprocess.Popen(sort_cmd, stdin=subprocess.PIPE, 
                                stdout=subprocess.PIPE, bufsize=1)
    genome_build = "b36"
    b36_list = ["hg18", "36", "b36", "build36", "NCBI36"]
    b37_list = ["hg19", "37", "b37", "build37", "GRCh37"]
    for line in gff_input:
        if not header_done:
            if re.match('#', line):
                header.append(line)
                if line.startswith("##genome-build"):
                    gbdata = line.split()
                    if len(gbdata) < 2:
                        raise Exception("no genome build specified?")
                    elif gbdata[1] in b36_list:
                        genome_build = "b36"
                    elif gbdata[1] in b37_list:
                        genome_build = "b37"
                    else:
                        raise Exception("genome build uninterpretable")
            else:
                header_done = True
        else:
            sort_out.stdin.write(str(line.rstrip('\n')) + '\n')
    sort_out.stdin.close()

    # Yield the genome build, followed by the GFF data.
    yield genome_build
    for line in header:
        yield line.rstrip('\n')
    for line in sort_out.stdout:
        yield line.rstrip('\n')

def genome_analyzer(genotype_file, server=None):
    """Perform analyses on genotype_file"""
    if server:
        server.server_close()
    # Set all the variables we'll use.
    input_dir = os.path.dirname(genotype_file)
    output_dir = input_dir + "-out"
    try:
        os.mkdir (output_dir)
    except OSError:
        pass
    lockfile = os.path.join(output_dir, "lock")
    logfile = os.path.join(output_dir, "log")
    log_handle = open(lockfile, "a+", 0)
    try:
        fcntl.flock(log_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print 'Lockfile really is locked.  Quitting.'
        return
    log_handle.seek(0)
    log_handle.truncate(0)
    log = Logger(log_handle)

    os.close(sys.stderr.fileno())
    os.close(sys.stdout.fileno())
    os.close(sys.stdin.fileno())
    os.dup2(log_handle.fileno(), sys.stderr.fileno())
    os.dup2(log_handle.fileno(), sys.stdout.fileno())

    # Set up arguments used by processing commands and scripts.
    # TODO: Fix getev_flat so it's stored somewhere more consistent with the
    # other data files. Probably "make daily" stuff should be going to 'DATA'
    # instead or in addition to public_html, but www-data would need to own it.
    args = { 'genotype_input': str(genotype_file),
             'miss_out': os.path.join(output_dir, 'missing_coding.json'),
             'sorted_out': os.path.join(output_dir, 'source_sorted.gff.gz'),
             'nonsyn_out': os.path.join(output_dir, 'ns.gff.gz'),
             'getev_out': os.path.join(output_dir, 'get-evidence.json'),
             'metadata_out': os.path.join(output_dir, 'metadata.json'),
             'genome_stats': os.path.join(os.getenv('DATA'), 'genome_stats.txt'),
             'genetests': os.path.join(os.getenv('DATA'), GENETESTS_DATA),
             'getev_flat': os.path.join(os.getenv('DATA'), GETEV_FLAT) 
             }

    # Make output directory if needed
    try:
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
    except:
        print "Unexpected error:", sys.exc_info()[0]

    # Process and sort input genome data
    log.put ('#status 0/100 calling process_source to process and sorting'
             'input file')
    gff_in_gen = process_source(args['genotype_input'])
    genome_data = { 'build': gff_in_gen.next() }

    # Set up build-dependent file locations
    if (genome_data['build'] == "b36"):
        args['dbsnp'] = os.path.join(os.getenv('DATA'), DBSNP_B36_SORTED)
        args['reference'] = os.path.join(os.getenv('DATA'), 
                                         REFERENCE_GENOME_HG18)
        args['transcripts'] = os.path.join(os.getenv('DATA'), 
                                           KNOWNGENE_HG18_SORTED)
    elif (genome_data['build'] == "b37"):
        args['dbsnp'] = os.path.join(os.getenv('DATA'), DBSNP_B37_SORTED)
        args['reference'] = os.path.join(os.getenv('DATA'), 
                                         REFERENCE_GENOME_HG19)
        args['transcripts'] = os.path.join(os.getenv('DATA'), 
                                           KNOWNGENE_HG19_SORTED)
    else:
        raise Exception("genome build data is invalid")

    # It might be more elegant to extract this from metadata.
    chrlist = ['chr' + str(x) for x in range(1, 22) + ['X', 'Y']]

    # Process genome through a series of GFF-formatted string generators.
    log.put('#status 4 processing genome data (get reference alleles, '
            + 'dbSNP IDs, nonsynonymous changes, etc.)')
    progtrack = ProgressTracker(log_handle, [5, 99], expected=chrlist, 
                                metadata=genome_data)
    # Record chromosomes seen and genome coverage.
    metadata_gen = get_metadata.genome_metadata(gff_in_gen,
                                                args['genome_stats'],
                                                progresstracker=progtrack)
    # Report coding regions that lack coverage.
    missing_gen = call_missing.report_uncovered(metadata_gen,
                                                args['transcripts'], 
                                                args['genetests'], 
                                                output_file=args['miss_out'],
                                                progresstracker=progtrack)
    # Find reference allele.
    twobit_gen = gff_twobit_query.match2ref(missing_gen, args['reference'])
    # Look up dbSNP IDs
    dbsnp_gen = gff_dbsnp_query.match2dbSNP(twobit_gen, args['dbsnp'])
    # Check for nonsynonymous SNP
    nonsyn_gen = gff_nonsynonymous_filter.predict_nonsynonymous(dbsnp_gen, 
                                                           args['reference'], 
                                                           args['transcripts'])
    # Pull off GET-Evidence hits
    nonsyn_gen2 = gff_getevidence_map.match_getev(nonsyn_gen, 
                                       args['getev_flat'], 
                                       output_file=args['getev_out'] + ".tmp", 
                                       progresstracker=progtrack)

    # Printing to output, pulls data through the generator chain.
    ns_out = gzip.open(args['nonsyn_out'], 'w')
    for line in nonsyn_gen2:
        ns_out.write(line + "\n")
    ns_out.close()
    os.system("mv " + args['getev_out'] + ".tmp " + args['getev_out'])

    # Print metadata
    metadata_f_out = open(args['metadata_out'], 'w')
    progtrack.write_metadata(metadata_f_out)
    metadata_f_out.close()

    log.put ('#status 100 finished')

    os.rename(lockfile, logfile)
    log_handle.close()
    print "Finished processing file " + str(genotype_file)


def main():
    """Genome analysis XMLRPC server, or submit analysis on command line"""
    # Parse options.
    usage = ("To run as XMLRPC server:\n%prog [--pidfile=PID_PATH " +
             "--stderr=STDERR_PATH --host=HOST_STRING --port=PORT_NUM\n"
             "To run on command line:\n%prog -g GENOME_DATA")
    parser = OptionParser(usage=usage)
    parser.add_option("-s", "--server", action="store_true", dest="is_server",
                      default=False, help="run as XML-RPC server")
    parser.add_option("--pidfile", dest="pidfile",
                      help="store PID in PID_FILE",
                      metavar="PID_FILE")
    parser.add_option("--stderr", dest="stderr",
                      help="write progress to LOG_FILE",
                      metavar="LOG_FILE")
    parser.add_option("--host", dest="host",
                      help="HOST on which to listen",
                      metavar="HOST")
    parser.add_option("-p", "--port", dest="port",
                      help="PORT on which to listen",
                      metavar="PORT")
    parser.add_option("-g", "--genome", dest="genome_data",
                      help="GENOME_DATA to process",
                      metavar="GENOME_DATA")
    option, args = parser.parse_args()
    
    if option.genome_data and not option.is_server:
        genome_analyzer(option.genome_data)
    elif option.is_server:
        if option.stderr:
            errout = open(option.stderr, 'a+', 0)
            os.dup2 (errout.fileno(), sys.stdout.fileno())
            os.dup2 (errout.fileno(), sys.stderr.fileno())

        if option.pidfile:
            file(option.pidfile, 'w+').write("%d\n" % os.getpid())

        # figure out the host and port
        host = option.host or "localhost"
        port = int(option.port or 8080)
        
        # create server
        server = SimpleXMLRPCServer((host, port))
        server.register_introspection_functions()
        
        def submit_local(genotype_file):
            """Start subprocess to perform genome analysis"""
            process = multiprocessing.Process(target=genome_analyzer, 
                                        args=(genotype_file,server,))
            process.start()
            print("Job submitted for genotype_file: \'" + 
                  str(genotype_file) + "\', process ID: \'" + 
                  str(process.pid) + "\'")
            return str(process.pid)

        server.register_function(submit_local)

        # run the server's main loop
        try:
            server.serve_forever()
        except:
            server.server_close()
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
