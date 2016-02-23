# Copyright (c) 2013-2016 Christian Geier et al.
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.


"""
CalendarCollection should enable modifying and querying a collection of
calendars. Each calendar is defined by the contents of a vdir, but uses an
SQLite db for caching (see backend if you're interested).
"""
import datetime
import os
import os.path
import itertools
import math

from vdirsyncer.storage.filesystem import FilesystemStorage
from vdirsyncer.exceptions import AlreadyExistingError

from . import backend
from .event import Event
from .. import log
from .exceptions import CouldNotCreateDbDir, UnsupportedFeatureError, \
    ReadOnlyCalendarError, UpdateFailed, DuplicateUid

logger = log.logger


def create_directory(path):
    if not os.path.isdir(path):
        if os.path.exists(path):
            raise RuntimeError('{0} is not a directory.'.format(path))
        try:
            os.makedirs(path, mode=0o750)
        except OSError as error:
            logger.fatal('failed to create {0}: {1}'.format(path, error))
            raise CouldNotCreateDbDir()


class CalendarCollection(object):
    """CalendarCollection allows access to various calendars stored in vdirs

    all calendars are cached in an sqlitedb for perforamce reasons"""

    def __init__(self,
                 calendars=None,
                 hmethod='fg',
                 default_color='',
                 multiple='',
                 color='',
                 highlight_event_days=0,
                 locale=None,
                 dbpath=None,
                 ):
        assert dbpath is not None
        assert calendars is not None
        self._calendars = calendars
        self._default_calendar_name = None
        self._storages = dict()
        for name, calendar in self._calendars.items():
            ctype = calendar.get('ctype', 'calendar')
            if ctype == 'calendar':
                file_ext = '.ics'
            elif ctype == 'birthdays':
                file_ext = '.vcf'
            else:
                raise ValueError('ctype must be either `calendar` or `birthdays`')
            self._storages[name] = FilesystemStorage(calendar['path'], file_ext)
        self.hmethod = hmethod
        self.default_color = default_color
        self.multiple = multiple
        self.color = color
        self.highlight_event_days = highlight_event_days
        self._locale = locale
        self._backend = backend.SQLiteDb(
            calendars=self.names, db_path=dbpath, locale=self._locale)
        self.update_db()

    @property
    def writable_names(self):
        return [c for c in self._calendars if not self._calendars[c].get('readonly', False)]

    @property
    def calendars(self):
        return self._calendars.values()

    @property
    def names(self):
        return self._calendars.keys()

    @property
    def default_calendar_name(self):
        return self._default_calendar_name

    @default_calendar_name.setter
    def default_calendar_name(self, default):
        if default is None:
            self._default_calendar_name = default
        elif default not in self.names:
            raise ValueError('Unknown calendar: {0}'.format(default))

        readonly = self._calendars[default].get('readonly', False)

        if not readonly:
            self._default_calendar_name = default
        else:
            raise ValueError(
                'Calendar "{0}" is read-only and cannot be used as default'.format(default))

    def _local_ctag(self, calendar):
        stat = os.stat(self._calendars[calendar]['path'])
        mtime = getattr(stat, 'st_mtime_ns', None)
        if mtime is None:
            mtime = stat.st_mtime
        return str(int(math.floor(mtime * 1e9)))

    def _cover_event(self, event):
        event.color = self._calendars[event.calendar]['color']
        event.readonly = self._calendars[event.calendar]['readonly']
        event.unicode_symbols = self._locale['unicode_symbols']
        return event

    def get_floating(self, start, end, minimal=False):
        events = self._backend.get_floating(start, end, minimal)
        return (self._cover_event(event) for event in events)

    def get_localized(self, start, end, minimal=False):
        events = self._backend.get_localized(start, end, minimal)
        return (self._cover_event(event) for event in events)

    def merge_events(self, floating, localized):

        """Merges two event generators into one while preserving order."""

        try:
            a = next(floating)
        except StopIteration:
            a = None
        try:
            b = next(localized)
        except StopIteration:
            b = None
        while (a is not None) or (b is not None):
            if a is not None and (b is None or a < b):
                yield a
                try:
                    a = next(floating)
                except StopIteration:
                    a = None
            else:
                yield b
                try:
                    b = next(localized)
                except StopIteration:
                    b = None

    def get_events_on(self, day, minimal=False):
        """return all events on `day`

        :param day: datetime.date
        :rtype: list()
        """
        start = datetime.datetime.combine(day, datetime.time.min)
        end = datetime.datetime.combine(day, datetime.time.max)
        floating_events = self.get_floating(start, end, minimal)
        localize = self._locale['local_timezone'].localize
        localized_events = self.get_localized(localize(start), localize(end), minimal)

        return self.merge_events(floating_events, localized_events)

    def get_events_at(self, dtime=datetime.datetime.now()):
        """get all events at datetime `dtime`

        :type dtime: datetime.datetime
        """
        if dtime.tzinfo is None:
            naive_dtime = dtime
            local_dtime = self._locale['local_timezone'].localize(dtime)
        else:
            naive_dtime = dtime.replace(tzinfo=None)
            local_dtime = dtime

        floating_events = self._backend.get_floating_at(naive_dtime)
        localized_events = self._backend.get_localized_at(local_dtime)
        return (self._cover_event(event) for event in
                itertools.chain(floating_events, localized_events))

    def update(self, event):
        """update `event` in vdir and db"""
        assert event.etag
        if self._calendars[event.calendar]['readonly']:
            raise ReadOnlyCalendarError()
        with self._backend.at_once():
            event.etag = self._storages[event.calendar].update(event.href, event, event.etag)
            self._backend.update(event.raw, event.href, event.etag, calendar=event.calendar)
            self._backend.set_ctag(self._local_ctag(event.calendar), calendar=event.calendar)

    def force_update(self, event, collection=None):
        """update `event` even if an event with the same uid/href already exists"""
        calendar = collection if collection is not None else event.calendar
        if self._calendars[calendar]['readonly']:
            raise ReadOnlyCalendarError()

        with self._backend.at_once():
            try:
                href, etag = self._storages[calendar].upload(event)
            except AlreadyExistingError as error:
                href = error.existing_href
                _, etag = self._storages[calendar].get(href)
                etag = self._storages[calendar].update(href, event, etag)
            self._backend.update(event.raw, href, etag, calendar=calendar)
            self._backend.set_ctag(self._local_ctag(calendar), calendar=calendar)

    def new(self, event, collection=None):
        """save a new event to the vdir and the database

        param event: the event that should be updated
        type event: event.Event
        """
        calendar = collection if collection is not None else event.calendar
        if hasattr(event, 'etag'):
            assert not event.etag
        if self._calendars[calendar]['readonly']:
            raise ReadOnlyCalendarError()

        with self._backend.at_once():

            try:
                href, etag = self._storages[calendar].upload(event)
            except AlreadyExistingError as Error:
                href = getattr(Error, 'existing_href', None)
                raise DuplicateUid(href)
            self._backend.update(event.raw, href, etag, calendar=calendar)
            self._backend.set_ctag(self._local_ctag(calendar), calendar=calendar)

    def delete(self, href, etag, calendar):
        if self._calendars[calendar]['readonly']:
            raise ReadOnlyCalendarError()
        self._storages[calendar].delete(href, etag)
        self._backend.delete(href, calendar=calendar)

    def get_event(self, href, calendar):
        return self._cover_event(self._backend.get(href, calendar))

    def change_collection(self, event, new_collection):
        href, etag, calendar = event.href, event.etag, event.calendar
        event.etag = None
        self.new(event, new_collection)
        self.delete(href, etag, calendar=calendar)

    def new_event(self, ical, collection):
        """creates and returns (but does not insert) new event from ical
        string"""
        calendar = collection or self.writable_names[0]
        return Event.fromString(ical, locale=self._locale, calendar=calendar)

    def update_db(self):
        """update the db from the vdir,

        should be called after every change to the vdir
        """
        for calendar in self._calendars:
            if self._needs_update(calendar):
                self._db_update(calendar)

    def _needs_update(self, calendar):
        """checks if the db for the given calendar needs an update"""
        return self._local_ctag(calendar) != self._backend.get_ctag(calendar)

    def _db_update(self, calendar):
        """implements the actual db update on a per calendar base"""
        db_hrefs = set(href for href, etag in self._backend.list(calendar))
        storage_hrefs = set()

        with self._backend.at_once():
            for href, etag in self._storages[calendar].list():
                storage_hrefs.add(href)
                db_etag = self._backend.get_etag(href, calendar=calendar)
                if etag != db_etag:
                    logger.debug('Updating {0} because {1} != {2}'.format(href, etag, db_etag))
                    self._update_vevent(href, calendar=calendar)
            for href in db_hrefs - storage_hrefs:
                self._backend.delete(href, calendar=calendar)
            self._backend.set_ctag(self._local_ctag(calendar), calendar=calendar)

    def _update_vevent(self, href, calendar):
        """should only be called during db_update, only updates the db,
        does not check for readonly"""
        event, etag = self._storages[calendar].get(href)
        try:
            if self._calendars[calendar].get('ctype') == 'birthdays':
                update = self._backend.update_birthday
            else:
                update = self._backend.update
            update(event.raw, href=href, etag=etag, calendar=calendar)

            return True
        except Exception as e:
            if not isinstance(e, (UpdateFailed, UnsupportedFeatureError)):
                logger.exception('Unknown exception happened.')
            logger.warning(
                'Skipping {0}/{1}: {2}\n'
                'This event will not be available in khal.'.format(calendar, href, str(e)))
            return False

    def search(self, search_string):
        """search for the db for events matching `search_string`"""
        return (self._cover_event(event) for event in self._backend.search(search_string))

    def get_day_styles(self, day, focus):
        devents = list(self.get_events_on(day, minimal=True))
        if len(devents) == 0:
            return None
        if self.color != '':
            return 'highlight_days_color'
        if len(devents) == 1:
            return 'calendar ' + devents[0].calendar
        if self.multiple != '':
            return 'highlight_days_multiple'
        return ('calendar ' + devents[0].calendar, 'calendar ' + devents[1].calendar)

    def get_styles(self, date, focus):
        if focus:
            if date == date.today():
                return 'today focus'
            else:
                return 'reveal focus'
        else:
            if date == date.today():
                return 'today'
            else:
                if self.highlight_event_days:
                    return self.get_day_styles(date, focus)
                else:
                    return None
