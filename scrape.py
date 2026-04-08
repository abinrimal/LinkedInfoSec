#!/usr/bin/env python3
"""
LinkedInfoSec – scrape.py
=========================
Scrapes LinkedIn's public job search results for InfoSec-related job postings
and extracts certification requirements from each listing.

For each job it:
  1. Collects card-level metadata (ID, title, location, age) via Selenium.
  2. Fetches the public job page over HTTP and parses the description for
     certification keywords.
  3. Records the job description snippet, open date (relative age), and any
     application close date mentioned in the listing text.
  4. Writes per-job rows to *_allinfo.csv and per-cert rows to *_data.csv.
  5. Produces *_all_certs.csv with a ranked count of every cert seen.

Original project by ahessmat: https://github.com/ahessmat/LinkedInfoSec
Extended with multi-browser support, web UI integration, macOS compatibility
fixes, description/date extraction, and stale-element resilience.
"""

from selenium import webdriver
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.chrome.service import Service as ChromeService
#from selenium.webdriver.firefox.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import StaleElementReferenceException, NoSuchElementException
import requests
import time
import pandas as pd
import re	#needed for regex calls
from tqdm import tqdm	#needed for loading bar on CLI
import argparse	#needed for parsing commandline arguments
import csv	#for writing to file
import html as html_lib
from pathlib import Path
import os
import platform

#Helper functions

purge_these_certs = []

def ask_user(question):
	answer = input(question + " (y/n): ").lower().strip()
	print("")
	while not(answer == "y" or answer == "yes" or answer == "n" or answer == "no"):
		print("Input yes or no")
		answer = input(question + "(y/n): ").lower().strip()
		print("")
	if answer[0] == "y":
		return True
	else:
		return False
		
def restricted_float(x):
	try:
		x = float(x)
	except ValueError:
		raise argparse.ArgumentTypeError("%r not a floating-point literal" % (x,))
	return x

def write_results_to_file(filename, j_id, j_title, j_certs, j_desc="", j_open_date="", j_close_date=""):
	"""Append one row to *_allinfo.csv.

	Columns: job_id, title, certs_set, description, open_date, close_date.
	Using csv.writer ensures values with commas or newlines are quoted properly.
	"""
	with open(f'{filename}_allinfo.csv', 'a+', newline='') as f:
		writer = csv.writer(f)
		writer.writerow([j_id[0], j_title[0], str(j_certs), j_desc, j_open_date, j_close_date])

def write_csv(filename, j_id, j_title, cert):
	"""Append one flat (job_id, title, cert, job_url) row to *_data.csv.

	This file has one row per cert per job, making it easy to pivot or filter
	in Excel / pandas without expanding sets.
	"""
	job_url = f"https://www.linkedin.com/jobs/view/{j_id}"
	with open(f'{filename}_data.csv', 'a+', newline='') as f:
		writer = csv.writer(f)
		writer.writerow([j_id, j_title, cert, job_url])
		
def store_dict(filename, dic):
	"""Write a certification-count dict to *_certs.csv.

	Called twice: once at the 50-job checkpoint and once at the end, so you get
	an early snapshot and the full ranked list.
	"""
	with open(f'{filename}_certs.csv', 'a+') as f:
		for key in dic.keys():
			f.write("%s,%s\n"%(key,dic[key]))
		

#Configure command line arguments
argp = argparse.ArgumentParser(description="Web Scraper meant for scraping certification information as it relates to InfoSec jobs off of LinkedIn")
argp.add_argument("-j", "--job", help="The job title or keyword to search for", default="cybersecurity")
argp.add_argument("-t", "--time", choices=['day', 'week', 'month', 'all'], help="How recent the listings to be scraped should be", default="all")
argp.add_argument("-s", "--seniority", type=list, help="The levels of seniority (1-5, least to greatest) to process as input: each level should be explicitly named for inclusion (e.g. for all levels, input is '12345'", default="1")
argp.add_argument("-l", "--location", help="The geographic area to consider jobs. Default is 'remote'", default="remote")
argp.add_argument("-i", "--increment", help="The increment of time in seconds that should be allowed to let jobs load for scraping", type=restricted_float, default=0.5)
argp.add_argument("-o", "--output", help="The name of the file to output scrape results to")
argp.add_argument("-q", "--quick", help="Only parse the first 100 results", action='store_true')
argp.add_argument("-k", "--keywords", help="A list of keywords to more narrowly filter LinkedIn's search results; excludes any job titles that do NOT have any of the listed keywords", default="")
argp.add_argument("--max", help="The maximum number of jobs that should be processed", type=int)
argp.add_argument("-b", "--browser", choices=['firefox', 'chrome'], help="Browser to run Selenium with", default="firefox")
argp.add_argument("--keep-open", help="Keep browser open after script completes", action='store_true')



parsed=argp.parse_args()

timeDic = {
	"day": "r86400",
	"week": "r604800",
	"month": "r2592000",
	"all" : ""
}

timeWindow = timeDic[parsed.time]

seniority = ','.join(parsed.seniority)

filterwords = (parsed.keywords).lower().split()

IBlack="\033[0;90m"       # Black
IRed="\033[0;91m"         # Red
IGreen="\033[0;92m"       # Green
IYellow="\033[0;93m"      # Yellow
IBlue="\033[0;94m"        # Blue
IPurple="\033[0;95m"      # Purple
ICyan="\033[0;96m"        # Cyan
IWhite="\033[0;97m"       # White

cert_dic = {}

# Well-known InfoSec cert names used as a regex-independent fallback.
# When the cert keyword regex misses an acronym (e.g. because the
# surrounding text lacks a "certification" trigger word), we do a plain
# uppercase string search against these known names as a safety net.
common_certs = [
	"CISSP", "CISM", "CISA", "CEH", "OSCP", "OSCE", "OSWP", "SECURITY+", "NETWORK+",
	"CASP+", "PENTEST+", "CCSP", "SSCP", "GSEC", "GPEN", "GWAPT", "CRISC", "GIAC",
	"AZ-500", "SC-200", "SC-300", "SC-100", "AWS SECURITY SPECIALTY", "CCNA", "CCNP"
]


def cert_appears_as_token(page_text_upper, cert_name):
	"""Return True when cert_name appears as a standalone token in page text."""
	pattern = rf"(?<![A-Z0-9]){re.escape(cert_name.upper())}(?![A-Z0-9])"
	return re.search(pattern, page_text_upper) is not None


try:
	#keyword 'cybersecurity' located 'remote'
	#cyber_url = 'https://www.linkedin.com/jobs/search?keywords=Cybersecurity&location=remote&geoId=&trk=public_jobs_jobs-search-bar_search-submit&position=1&pageNum=0'
	
	cyber_url = f'https://www.linkedin.com/jobs/search?keywords={parsed.job}&location={parsed.location}&geoId=&trk=public_jobs_jobs-search-bar_search-submit&f_TPR={timeWindow}&f_E={seniority}&position=1&pageNum=0'
	
	#pentester
	#cyber_url = 'https://www.linkedin.com/jobs/search?keywords=pentester&location=United%20States&geoId=&trk=public_jobs_jobs-search-bar_search-submit&position=1&pageNum=0'

	#fireFoxOptions = Options()
	#fireFoxOptions.add_argument("--headless")
	#ffoptions = webdriver.FirefoxOptions()
	#ffoptions.set_headless()
	#ffoptions.add_argument("--headless")
	#wd = webdriver.Firefox(executable_path='/home/kali/LinkedInfoSec/geckodriver')
	#wd = webdriver.Firefox(firefox_options=ffoptions)
	# --- Browser initialisation ---
	# Selenium Manager (bundled with selenium>=4.6) auto-downloads the matching
	# driver when no local binary is provided, so we only use the bundled
	# geckodriver if it is actually executable on this platform.
	if parsed.browser == 'firefox':
		local_driver = Path(__file__).resolve().parent / 'geckodriver'
		use_local_driver = local_driver.exists() and os.access(local_driver, os.X_OK)

		# macOS cannot execute Linux ELF binaries; skip the bundled driver in that case.
		if use_local_driver and platform.system() == 'Darwin':
			with open(local_driver, 'rb') as driver_file:
				is_elf = driver_file.read(4) == b'\x7fELF'
			if is_elf:
				use_local_driver = False

		service = FirefoxService(executable_path=str(local_driver)) if use_local_driver else FirefoxService()
		wd = webdriver.Firefox(service=service)
	else:
		chrome_options = webdriver.ChromeOptions()
		# detach=True keeps the Chrome window open after the script exits,
		# useful for inspecting the final page state.
		if parsed.keep_open:
			chrome_options.add_experimental_option("detach", True)
		service = ChromeService()
		wd = webdriver.Chrome(service=service, options=chrome_options)

	#Getting cyber-related jobs
	wd.get(cyber_url)

	#This pulls the total number of search results from the DOM, strips the "+" and ',' characters, then casts as an int
	str_of_cyberjobs = str(wd.find_element(By.CSS_SELECTOR,'h1>span').get_attribute('innerText'))
	str_of_cyberjobs = str_of_cyberjobs.replace('+','')
	no_cyberjobs = int(str_of_cyberjobs.replace(',', ''))
	if parsed.max is not None and no_cyberjobs > parsed.max:
		no_cyberjobs = parsed.max

	#print(f"# of cybersecurity jobs: {no_cyberjobs}")
	
	"""
	question = f"The certscraper has found {no_cyberjobs} to scrape, do you want to proceed?"
	if not ask_user(question):
		exit()
	"""
	
	# --- Scroll-to-load: force all job cards into the DOM ---
	# LinkedIn renders listings lazily in batches of 25. We scroll to the bottom
	# on each iteration and click "See more jobs" when it appears. In quick mode
	# we only do one scroll pass (≈100 results).
	jobs_iteration = 1 if parsed.quick else (no_cyberjobs // 25) + 1
	for i in tqdm(range(jobs_iteration)):
		wd.execute_script('window.scrollTo(0,document.body.scrollHeight);')
		try:
			# Click the "See More Jobs" button when it becomes visible.
			found = len(wd.find_elements(By.CLASS_NAME, 'infinite-scroller__show-more-button--visible'))
			if found > 0:
				wd.find_element(By.CLASS_NAME, 'infinite-scroller__show-more-button--visible').click()
			else:
				time.sleep(1)
		except:
			time.sleep(1)
			pass
			
	#Find all jobs
	job_list = wd.find_element(By.CLASS_NAME, 'jobs-search__results-list')
	#Look at each job
	jobs = job_list.find_elements(By.TAG_NAME, 'li')
	print(f'The number of jobs actually to be processed: {len(jobs)}')
	
	job_id = []
	job_title = []
	job_location = []
	job_age = []
	job_num = 1
	failed_jobs = 0	#The number of jobs that failed to load for web scraping
	no_certs = 0	#the number of jobs scraped where certs weren't found
	yes_certs = 0	#The number of jobs scraped where certs were found
	may_certs = 0	#The number of jobs scraped where certs may exist with closer inspection
	job_tracker = 0 #Tracks how many jobs we've observed so far
	
	#cert_dic = {}
	
	#TODO, fix tqdm to reflect -q option
	#TODO, implement interrupt function to allow user to change speed of jobs processing on-the-fly
	
	# --- Per-job processing loop ---
	# LinkedIn's DOM can refresh mid-iteration (e.g. when a card is lazily
	# rendered or the sidebar updates), invalidating previously held element
	# references. To avoid StaleElementReferenceException we re-fetch the full
	# list on every iteration rather than holding onto a stale list of elements.
	for idx in tqdm(range(len(jobs))):
		try:
			job_list = wd.find_element(By.CLASS_NAME, 'jobs-search__results-list')
			job_rows = job_list.find_elements(By.TAG_NAME, 'li')
			if idx >= len(job_rows):
				failed_jobs += 1
				continue
			job = job_rows[idx]
		except (StaleElementReferenceException, NoSuchElementException):
			failed_jobs += 1
			continue

		#print(IWhite + f"\n[+] Now listing JOBID: {job_id}, {job_title}:")
		
		#Pulling information from the job description by clicking through each job
		#Reference for fetching XPATH: https://www.guru99.com/xpath-selenium.html
		"""job_link = f"/html/body/div[1]/div/main/section[2]/ul/li[{job_num}]/*"
		#print(job_link)
		try:
			wd.find_element(By.XPATH, job_link).click()
		except:
			job_num += 1
			continue"""
		#The first several jobs are particularly relevant to the -j flag
		#Subsequent results are less important and don't need as much attention
		"""if job_num < 50:
			#WebDriverWait(wd, 10).until(EC.element_to_be_clickable((By.XPATH, "/html/body/div[1]/div/section/div[2]/section/div/div[1]/div/div/a")))
			#time.sleep(2)
			
			try:
				#WebDriverWait(wd, parsed.increment).until(EC.element_to_be_clickable((By.XPATH, "/html/body/div[1]/div/section/div[2]/section/div/div[1]/div/div/a")))
				WebDriverWait(wd, parsed.increment).until(EC.element_to_be_clickable((By.XPATH, "/html/body/div[1]/div/section/div[2]/section/div/div[1]/div/div/button[1]")))
			except:
				job_num += 1
				print(IRed + f"[-] Failed to render job listing. Try slowing down rate with -i." + IWhite)
				continue
			
		else:
			if parsed.quick:
				break
			#time.sleep(parsed.increment)
			try:
				#WebDriverWait(wd, {parsed.increment}).until(EC.element_to_be_clickable((By.XPATH, "/html/body/div[1]/div/section/div[2]/section/div/div[1]/div/a")))
				WebDriverWait(wd, parsed.increment).until(EC.element_to_be_clickable((By.XPATH, "/html/body/div[0]/div/section/div[1]/section")))
			except:
				print("F2")
				job_num += 1
				continue"""
		#print("IT WORKED")
		try:
			j_id = job.find_element(By.CLASS_NAME, 'job-search-card').get_attribute('data-entity-urn')
			j_id = str(j_id).replace('urn:li:jobPosting:','')
			j_title = job.find_element(By.CLASS_NAME, 'base-search-card__title').get_attribute('innerText')
			j_location = job.find_element(By.CLASS_NAME, 'job-search-card__location').get_attribute('innerText')
			j_age = job.find_element(By.TAG_NAME, 'time').get_attribute('innerText')
		except (StaleElementReferenceException, NoSuchElementException):
			failed_jobs += 1
			continue
		job_id.append(j_id)
		job_title.append(j_title)
		job_location.append(j_location)
		job_age.append(j_age)
		job_num = job_num + 1

		# Apply keyword filter (-k flag): skip any job whose title contains none
		# of the required keywords. Useful to narrow broad searches like
		# "cybersecurity" down to "SOC analyst" or "cloud security" etc.
		if len(filterwords) > 0:
			t_title = j_title.lower()
			isGood = any(kword in t_title for kword in filterwords)
			if not isGood:
				continue
		#job_desc_block = "/html/body/div[1]/div/section/div[2]/div[1]/section[1]/div/div[2]/section/div"
		#print(job_num,j_id,j_title,j_age)
		j_desc = ""
		j_open_date = j_age
		j_close_date = ""		
		#This is the XPATH to the descriptive text
		job_desc_block = "/html/body/div[1]/div/section/div[2]/div/section[1]/div/div/section/div"
		#job_desc_block = "/html/body/div[1]/div/section/div[2]/div/section[2]/div/div/section/div"
				
		# Sometimes LinkedIn doesn't render the right detail pane in time.
		# Keep going and parse the public job page directly instead of skipping the job.
		found = len(wd.find_elements(By.XPATH, job_desc_block))
		if found > 0:
			jd_block = wd.find_element(By.XPATH, job_desc_block).get_attribute('innerHTML')
			#print(jd_block)
		else:
			failed_jobs += 1
		
		keywords = ["certification", "certifications", "certs", "Certification", "Certifications", "Certs", "accreditations", "accreditation", "Certification(s)", "certification(s)"]
		jd_certs = set()
		
		### FIND KEYWORDS ###
		# Replace this URL with the one you want to request
		#url = "https://www.linkedin.com/jobs/search?currentJobId=3716710886"
		j_url = "https://www.linkedin.com/jobs/view/" + j_id

		# Send a GET request to the URL
		headers = {
			"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
		}
		response = requests.get(j_url, headers=headers, timeout=20)
		foundcert = False

		# Check if the request was successful (status code 200)
		if response.status_code == 200:
			page_text = response.text
			page_text_upper = page_text.upper()

			# --- Extract description from the HTML markup section ---
			desc_m = re.search(
				r'class="show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>',
				page_text, re.DOTALL | re.I
			)
			if desc_m:
				raw_desc = desc_m.group(1)
				clean = re.sub(r'<[^>]+>', ' ', raw_desc)
				clean = html_lib.unescape(clean)
				clean = re.sub(r'\s+', ' ', clean).strip()
				j_desc = clean[:500]

			# --- Try to find a close/deadline date in the description or page text ---
			# LinkedIn doesn't expose deadlines in structured data; some employers include them in text.
			close_m = re.search(
				r'(?:clos(?:ing|es?)\s+(?:date|on)?|apply\s+(?:by|before)|application\s+(?:deadline|due)|'
				r'deadline[:\s])[\s:]*([A-Za-z][\w,\s]{3,30}?\d{4}|\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})',
				j_desc + " " + page_text, re.I
			)
			if close_m:
				j_close_date = close_m.group(1).strip()[:50]

			# Print the response content (the web page HTML, for example)
			#print(response.text)

			# Define the keywords you want to search for
			keywords = ["certification", "certifications", "certs", "Certification", "Certifications", "Certs", "accreditations", "accreditation", "Certification(s)", "certification(s)"]

			# Build a pattern that matches lines mentioning cert-related words.
			keyword_pattern = "|".join(re.escape(keyword) for keyword in keywords)
			pattern = re.compile(keyword_pattern, re.I)

			# Pattern to extract cert-like tokens: hyphenated uppercase codes
			# (AZ-500, SC-200), acronyms with '+' (SECURITY+, CASP+), or tokens
			# ending in 2+ uppercase letters (e.g. wordCISSP).
			word_pattern = re.compile(r"[A-Z]+\-+[\w]+|[a-zA-Z]+\+|[\w]+[A-Z]{2,}")

			
			# Iterate through the response content line by line
			for line in response.iter_lines(decode_unicode=True):
				if pattern.search(line):
					matching_words = word_pattern.findall(line)
					if matching_words:
						#print("Matching Line:", line)
						#print("Matching Words:", matching_words)
						#csvset = (matchin)
						foundcert = True

						for cred in set(matching_words):
							write_csv(parsed.output, j_id, j_title, cred)
						jd_certs.update(matching_words)

			# Fallback: match commonly requested cert names directly from full page text.
			for cert_name in common_certs:
				if cert_appears_as_token(page_text_upper, cert_name):
					foundcert = True
					jd_certs.add(cert_name)
					write_csv(parsed.output, j_id, j_title, cert_name)
		else:
			#print(f"Failed to retrieve the page. Status code: {response.status_code}")
			pass

		"""if (jd_certs == set()):
			no_certs += 1
		else:
			yes_certs += 1"""
		if foundcert:
			yes_certs += 1
		else:
			no_certs += 1
		
		
		
		job_tracker += 1
		write_results_to_file(parsed.output, [j_id], [j_title], jd_certs, j_desc, j_open_date, j_close_date)
		
		for cert in jd_certs:
			if cert in cert_dic:
				cert_dic[cert] += 1
			else:
				cert_dic[cert] = 1
				
		if job_tracker == 50:
			res = dict(sorted(cert_dic.items(), key=lambda x: (-x[1], x[0])))
			store_dict(f'{parsed.output}_first50', res)
		#print(IWhite + str(jd_certs))

	#wd.find_element(By.CLASS_NAME, 'jobs-search__results-list')
	#print(job_id)
	print(IYellow + f"[-] Job detail pane failed to render for {failed_jobs} listings; HTTP parsing fallback was still used.")
	print(IGreen + f"[+] A total of {no_certs} jobs did not have any certs found")
	print(IGreen + f"[+] A total of {yes_certs} jobs did have certs listed")
	
	res = dict(sorted(cert_dic.items(), key=lambda x: (-x[1], x[0])))
	store_dict(f'{parsed.output}_all', res)
	
	for k,v in res.items():
		print(f"{k} : {v}")
	print(f"{yes_certs} jobs had certs listed, {no_certs} did not list any certs or could not be loaded.")
except KeyboardInterrupt:
	print(IRed + "[-] CTRL+C detected! Terminating")
	res = dict(sorted(cert_dic.items(), key=lambda x: (-x[1], x[0])))

	for k,v in res.items():
		print(f"{k} : {v}")
	
finally:
	if not parsed.keep_open:
		try:
			wd.quit()
		except:
			pass
