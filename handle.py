#!/usr/bin/env python3
"""
LinkedInfoSec – handle.py
=========================
Post-processing script that reads a *_allinfo.csv file produced by scrape.py
and counts how often each certification token appears across all listed jobs.

Outputs a ranked dict to stdout, which app.py captures as the ``certs`` result
for a completed pipeline run.

Usage:
    python handle.py -f <output_prefix>_allinfo.csv

Original project by ahessmat: https://github.com/ahessmat/LinkedInfoSec
"""

import argparse	# needed for parsing commandline arguments
import os.path
import re

argp = argparse.ArgumentParser(description="Web Scraper meant for scraping certification information as it relates to InfoSec jobs off of LinkedIn")
argp.add_argument("-f", "--file", help="The filename to open and parse")
parsed=argp.parse_args()

filename = parsed.file

if not os.path.exists(filename):
	argp.error("File doesn't exist")

d = {}

# Each line in allinfo.csv contains a Python set literal in the certs column,
# e.g.  {"CISSP", "OSCP"}.  We extract every single-quoted token from those
# set strings and count occurrences across all jobs.
with open(f'{filename}', 'r') as f:
	lines = f.readlines()
	pattern = r"'([A-Za-z0-9_\./\\-]*)'"
	for line in lines:
		m = re.findall(pattern, line)
		if len(m) > 0:
			for e in m:
				if e in d:
					d[e] += 1
				else:
					d[e] = 1

res = dict(sorted(d.items(), key=lambda x: (-x[1], x[0])))
print(res)
		
	

"""
with open(f'pentester_certs.txt', 'r') as f:
	lines = f.readlines()
	certs = {}
	for line in lines:
		cert = line.split()
		for c in cert:
			if c in certs:
				certs[c] += 1
			else:
				certs[c] = 1
	res = dict(sorted(certs.items(), key=lambda x: (-x[1], x[0])))
	print(res)
"""