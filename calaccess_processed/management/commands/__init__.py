#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Base classes for custom management commands.
"""
import os
import re
import logging
from six.moves.urllib.parse import urljoin
from six.moves.urllib.request import url2pathname
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from django.conf import settings
from django.utils.termcolors import colorize
from calaccess_processed.decorators import retry
from django.core.management.base import BaseCommand
from opencivicdata.management.commands.loaddivisions import load_divisions
from opencivicdata.models import (
    Division,
    Jurisdiction,
    Organization,
    Person,
    Post,
    Candidacy,
)
from opencivicdata.models.elections import Election
logger = logging.getLogger(__name__)


class CalAccessCommand(BaseCommand):
    """
    Base class for all custom CalAccess-related management commands.
    """
    def handle(self, *args, **options):
        """
        Sets options common to all commands.

        Any command subclassing this object should implement its own
        handle method, as is standard in Django, and run this method
        via a super call to inherit its functionality.
        """
        # Set global options
        self.verbosity = options.get("verbosity")
        self.no_color = options.get("no_color")

        # Start the clock
        self.start_datetime = datetime.now()

    def header(self, string):
        """
        Writes out a string to stdout formatted to look like a header.
        """
        logger.debug(string)
        if not getattr(self, 'no_color', None):
            string = colorize(string, fg="cyan", opts=("bold",))
        self.stdout.write(string)

    def log(self, string):
        """
        Writes out a string to stdout formatted to look like a standard line.
        """
        logger.debug(string)
        if not getattr(self, 'no_color', None):
            string = colorize("%s" % string, fg="white")
        self.stdout.write(string)

    def success(self, string):
        """
        Writes out a string to stdout formatted green to communicate success.
        """
        logger.debug(string)
        if not getattr(self, 'no_color', None):
            string = colorize(string, fg="green")
        self.stdout.write(string)

    def warn(self, string):
        """
        Writes string to stdout formatted yellow to communicate a warning.
        """
        logger.warn(string)
        if not getattr(self, 'no_color', None):
            string = colorize(string, fg="yellow")
        self.stdout.write(string)

    def failure(self, string):
        """
        Writes string to stdout formatted red to communicate failure.
        """
        logger.error(string)
        if not getattr(self, 'no_color', None):
            string = colorize(string, fg="red")
        self.stdout.write(string)

    def duration(self):
        """
        Calculates how long command has been running and writes it to stdout.
        """
        duration = datetime.now() - self.start_datetime
        self.stdout.write('Duration: {}'.format(str(duration)))
        logger.debug('Duration: {}'.format(str(duration)))


class ScrapeCommand(CalAccessCommand):
    """
    Base management command for scraping the CAL-ACCESS website.
    """
    base_url = 'http://cal-access.ss.ca.gov/'
    cache_dir = os.path.join(
        settings.BASE_DIR,
        ".scraper_cache"
    )

    def add_arguments(self, parser):
        """
        Adds custom arguments specific to this command.
        """
        parser.add_argument(
            '--flush',
            action='store_true',
            dest='force_flush',
            default=False,
            help='Flush database tables',
        )
        parser.add_argument(
            '--force-download',
            action='store_true',
            dest='force_download',
            default=False,
            help='Force the scraper to download URLs even if they are cached',
        )
        parser.add_argument(
            '--cache-only',
            action='store_false',
            dest='update_cache',
            default=True,
            help="Skip the scraper's update checks. Use only cached files.",
        )

    def handle(self, *args, **options):
        """
        Make it happen.
        """
        super(ScrapeCommand, self).handle(*args, **options)

        self.force_flush = options.get("force_flush")
        self.force_download = options.get("force_download")
        self.update_cache = options.get("update_cache")

        os.path.exists(self.cache_dir) or os.mkdir(self.cache_dir)

        if self.force_flush:
            self.flush()
        results = self.scrape()
        self.save(results)

    @retry(requests.exceptions.RequestException)
    def get_url(self, url, retries=1, request_type='GET'):
        """
        Returns the response from a URL, retries if it fails.
        """
        headers = {
            'User-Agent': 'California Civic Data Coalition \
            (cacivicdata@gmail.com)',
        }
        if self.verbosity > 2:
            self.log(" Making a {} request for {}".format(request_type, url))
        return getattr(requests, request_type.lower())(url, headers=headers)

    def get_headers(self, url):
        """
        Returns a dict with metadata about the current CAL-ACCESS snapshot.
        """
        response = self.get_url(url, request_type='HEAD')
        try:
            length = int(response.headers['content-length'])
        except KeyError:
            length = None
        return {
            'content-length': length,
        }

    def get_html(self, url, retries=1, base_url=None):
        """
        Makes request for a URL and returns HTML as a BeautifulSoup object.
        """
        # Put together the full URL
        full_url = urljoin(base_url or self.base_url, url)
        if self.verbosity > 2:
            self.log(" Retrieving data for {}".format(url))

        # Pull a cached version of the file, if it exists
        cache_path = os.path.join(
            self.cache_dir,
            url2pathname(url.strip("/"))
        )
        if os.path.exists(cache_path) and not self.force_download:
            # Make a HEAD request for the file size of the live page
            if self.update_cache:
                cache_file_size = os.path.getsize(cache_path)
                head = self.get_headers(full_url)
                web_file_size = head['content-length']

                if self.verbosity > 2:
                    msg = " Cached file sized {}. Web file size {}."
                    self.log(msg.format(
                        cache_file_size,
                        web_file_size
                    ))

            # If our cache is the same size as the live page, return the cache
            if not self.update_cache or cache_file_size == web_file_size:
                if self.verbosity > 2:
                    self.log(" Returning cached {}".format(cache_path))
                html = open(cache_path, 'r').read()
                return BeautifulSoup(html, "html.parser")

        # Otherwise, retrieve the full page and cache it
        try:
            response = self.get_url(full_url)
        except requests.exceptions.HTTPError as e:
            # If web requests fails, fall back to cached file, if it exists
            if os.path.exists(cache_path):
                if self.verbosity > 2:
                    self.log(" Returning cached {}".format(cache_path))
                html = open(cache_path, 'r').read()
                return BeautifulSoup(html, "html.parser")
            else:
                raise e

        # Grab the HTML and cache it
        html = response.text
        if self.verbosity > 2:
            self.log(" Writing to cache {}".format(cache_path))
        cache_subdir = os.path.dirname(cache_path)
        os.path.exists(cache_subdir) or os.makedirs(cache_subdir)
        with open(cache_path, 'w') as f:
            f.write(html)

        # Finally return the HTML ready to parse with BeautifulSoup
        return BeautifulSoup(html, "html.parser")

    def flush(self):
        """
        This method should empty out database tables filled by this command.
        """
        raise NotImplementedError

    def scrape(self):
        """
        This method should perform the actual scraping.

        Returns the structured data.
        """
        raise NotImplementedError

    def save(self, results):
        """
        This method should process structured data returned by `build_results`.
        """
        raise NotImplementedError


class LoadOCDModelsCommand(CalAccessCommand):
    """
    Base class for OCD model loading management commands.
    """
    def handle(self, *args, **options):
        """
        Make it happen.
        """
        super(LoadOCDModelsCommand, self).handle(*args, **options)
        try:
            self.state_division = Division.objects.get(
                id='ocd-division/country:us/state:ca'
            )
        except Division.DoesNotExist:
            if self.verbosity > 2:
                self.log(' CA state division missing. Loading all U.S. divisions')
            load_divisions('us')
            self.state_division = Division.objects.get(
                id='ocd-division/country:us/state:ca'
            )
        self.state_jurisdiction = Jurisdiction.objects.get_or_create(
            name='California State Government',
            url='http://www.ca.gov',
            division=self.state_division,
            classification='government',
        )[0]
        self.executive_branch = Organization.objects.get_or_create(
            name='California State Executive Branch',
            classification='executive',
        )[0]
        self.sos = Organization.objects.get_or_create(
            name='California Secretary of State',
            classification='executive',
            parent=self.executive_branch,
        )[0]

    def create_election(self, name, date_obj):
        """
        Create an OCD Election object.
        """
        admin = Organization.objects.get_or_create(
            name='Elections Division',
            classification='executive',
            parent=self.sos,
        )[0]
        obj = Election.objects.create(
            start_time=date_obj,
            name=name,
            all_day=True,
            timezone='US/Pacific',
            classification='election',
            administrative_organization=admin,
            division=self.state_division,
            jurisdiction=self.state_jurisdiction,
        )
        return obj

    def parse_office_name(self, office_name):
        """
        Parse string containg the name for an office.

        Expected format is "{TYPE NAME}[{DISTRICT NUMBER}]".

        Return a dict with two keys: office and district.
        """
        office_pattern = r'^(?P<type>[A-Z ]+)(?P<district>\d{2})?$'
        parsed = re.match(office_pattern, office_name.upper()).groupdict()
        parsed['type'] = parsed['type'].strip()

        try:
            parsed['district'] = int(parsed['district'])
        except TypeError:
            pass

        return parsed

    def get_or_create_post(self, office_name):
        """
        Get or create a Post object with an office_name string.

        Returns a tuple (Post object, created), where created is a boolean
        specifying whether a Post was created.
        """
        parsed_office = self.parse_office_name(office_name)

        # prepare to get or create post
        raw_post = {'label': office_name.title().replace('Of', 'of')}

        if parsed_office['type'] == 'STATE SENATE':
            raw_post['division'] = Division.objects.get(
                subid1='ca',
                subtype2='sldu',
                subid2=str(parsed_office['district']),
            )
            raw_post['organization'] = Organization.objects.get_or_create(
                name='California State Senate',
                classification='upper',
            )[0]
            raw_post['role'] = 'Senator'
        elif parsed_office['type'] == 'ASSEMBLY':
            raw_post['division'] = Division.objects.get(
                subid1='ca',
                subtype2='sldl',
                subid2=str(parsed_office['district']),
            )
            raw_post['organization'] = Organization.objects.get_or_create(
                name='California State Assembly',
                classification='lower',
            )[0]
            raw_post['role'] = 'Assembly Member'
        else:
            # If not Senate or Assembly, assume this is a state office
            raw_post['division'] = self.state_division
            if parsed_office['type'] == 'MEMBER BOARD OF EQUALIZATION':
                raw_post['organization'] = Organization.objects.get_or_create(
                    name='State Board of Equalization',
                    parent=self.executive_branch,
                )[0]
                raw_post['role'] = 'Board Member'
            elif parsed_office['type'] == 'SECRETARY OF STATE':
                raw_post['organization'] = self.sos
                raw_post['role'] = raw_post['label']
            else:
                raw_post['organization'] = self.executive_branch
                raw_post['role'] = raw_post['label']

        return Post.objects.get_or_create(**raw_post)

    def get_or_create_person(self, name, filer_id=None):
        """
        Get or create a Person object with the name string and optional filer_id.

        If a filer_id is provided, first attempt to lookup the person by filer_id.

        If the person doesn't exist (or the filer_id is not provided), create a
        new Person.

        Returns a tuple (Person object, created), where created is a boolean
        specifying whether a Person was created.
        """
        person = None
        created = False

        if filer_id:
            if filer_id != '':
                try:
                    person = Person.objects.get(
                        identifiers__scheme='calaccess_filer_id',
                        identifiers__identifier=filer_id,
                    )
                except Person.DoesNotExist:
                    pass

        if not person:
            # split and flip the original name string
            split_name = name.split(',')
            split_name.reverse()
            person = Person.objects.create(
                sort_name=name,
                name=' '.join(split_name).strip()
            )
            if filer_id:
                person.identifiers.create(
                    scheme='calaccess_filer_id',
                    identifier=filer_id,
                )
            created = True

        return (person, created)

    def get_or_create_candidacy(self, contest_obj, filer_id=None, person_name=None):
        """
        Get or create a Candidacy object.

        First, lookup an existing Candidacy within the given CandidateContest linked
        to a Person with the given filer_id or person_name.

        If neither filer_id or person_name are provided, an exception is raised.

        If there's no existing Candidacy, a new one is created. A new Person is
        also created if there's no existing Person with the given filer_id, or no
        filer_id is provided.

        Returns a tuple (Candidacy object, created), where created is a boolean
        specifying whether a Candidacy was created.
        """
        if not filer_id and not person_name:
            raise Exception("Must provide either filer_id or person_name.")
        elif filer_id:
            person, person_created = self.get_or_create_person(
                person_name,
                filer_id=filer_id,
            )
            if person_created and self.verbosity > 2:
                self.log('Created new Person: %s' % person.name)
            candidacy, candidacy_created = contest_obj.candidacies.get_or_create(
                person=person,
                post=contest_obj.posts.all()[0].post,
                candidate_name=person_name,
                registration_status='qualified',
            )
        else:
            try:
                candidacy = contest_obj.candidacies.get(
                    post=contest_obj.posts.all()[0].post,
                    person__sort_name=person_name,
                )
            except Candidacy.DoesNotExist:
                person, person_created = self.get_or_create_person(
                    person_name,
                    filer_id=filer_id,
                )
                if person_created and self.verbosity > 2:
                    self.log('Created new Person: %s' % person.name)

                candidacy = contest_obj.candidacies.create(
                    person=person,
                    post=contest_obj.posts.all()[0].post,
                    candidate_name=person_name,
                    registration_status='qualified',
                )
                candidacy_created = True
            else:
                candidacy_created = False

        return (candidacy, candidacy_created)
