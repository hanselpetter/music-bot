import os
import json
import asyncio
import logging
import traceback

from enum import Enum
from .constructs import Serializable
from .exceptions import ExtractionError
from .utils import get_header, md5sum

log = logging.getLogger(__name__)


class EntryTypes(Enum):
    URL = 1
    STEAM = 2
    FILE = 3

    def __str__(self):
        return self.name


class BasePlaylistEntry(Serializable):
    def __init__(self):
        self.filename = None
        self._is_downloading = False
        self._waiting_futures = []

    @property
    def is_downloaded(self):
        if self._is_downloading:
            return False

        return bool(self.filename)

    async def _download(self):
        raise NotImplementedError

    def get_ready_future(self):
        """
        Returns a future that will fire when the song is ready to be played. The future will either fire with the result (being the entry) or an exception
        as to why the song download failed.
        """
        future = asyncio.Future()
        if self.is_downloaded:
            # In the event that we're downloaded, we're already ready for playback.
            future.set_result(self)

        else:
            # If we request a ready future, let's ensure that it'll actually resolve at one point.
            asyncio.ensure_future(self._download())
            self._waiting_futures.append(future)

        return future

    def _for_each_future(self, cb):
        """
            Calls `cb` for each future that is not cancelled. Absorbs and logs any errors that may have occurred.
        """
        futures = self._waiting_futures
        self._waiting_futures = []

        for future in futures:
            if future.cancelled():
                continue

            try:
                cb(future)

            except:
                traceback.print_exc()

    def __eq__(self, other):
        return self is other

    def __hash__(self):
        return id(self)


class URLPlaylistEntry(BasePlaylistEntry):
    def __init__(self, playlist, url, title, duration=0, expected_filename=None, **meta):
        super().__init__()

        self.playlist = playlist
        self.url = url
        self.title = title
        self.duration = duration
        self.expected_filename = expected_filename
        self.meta = meta

        self.download_folder = self.playlist.downloader.download_folder

    @classmethod
    def _deserialize(cls, data, playlist=None):
        assert playlist is not None, cls._bad('playlist')

        try:
            # TODO: version check
            url = data['url']
            title = data['title']
            duration = data['duration']
            downloaded = data['downloaded']
            filename = data['filename'] if downloaded else None
            expected_filename = data['expected_filename']
            meta = {}

            # TODO: Better [name] fallbacks
            if 'channel' in data['meta']:
                meta['channel'] = playlist.bot.get_channel(data['meta']['channel']['id'])

            if 'author' in data['meta']:
                meta['author'] = meta['channel'].server.get_member(data['meta']['author']['id'])

            entry = cls(playlist, url, title, duration, expected_filename, **meta)
            entry.filename = filename
            entry._is_downloading = False

            return entry
        except Exception as e:
            log.error("Could not load {}".format(cls.__name__), exc_info=e)


    def __json__(self):
        return self._enclose_json({
            'version': 1,
            'url': self.url,
            'title': self.title,
            'duration': self.duration,
            'downloaded': self.is_downloaded,
            'expected_filename': self.expected_filename,
            'filename': self.filename,
            # Todo: add server
            'meta': {
                name: {
                    'type': obj.__class__.__name__,
                    'id': obj.id,
                    'name': obj.name
                } for (name, obj) in self.meta.items()
            }
            # Actually I think I can just getattr(discord, type)
        })

    # noinspection PyTypeChecker
    async def _download(self):
        if self._is_downloading:
            return

        self._is_downloading = True
        try:
            # Ensure the folder that we're going to move into exists.
            if not os.path.exists(self.download_folder):
                os.makedirs(self.download_folder)

            # self.expected_filename: audio_cache\youtube-9R8aSKwTEMg-NOMA_-_Brain_Power.m4a
            extractor = os.path.basename(self.expected_filename).split('-')[0]

            # the generic extractor requires special handling
            if extractor == 'generic':
                flistdir = [f.rsplit('-', 1)[0] for f in os.listdir(self.download_folder)]
                expected_fname_noex, fname_ex = os.path.basename(self.expected_filename).rsplit('.', 1)

                if expected_fname_noex in flistdir:
                    try:
                        rsize = int(await get_header(self.playlist.bot.aiosession, self.url, 'CONTENT-LENGTH'))
                    except:
                        rsize = 0

                    lfile = os.path.join(
                        self.download_folder,
                        os.listdir(self.download_folder)[flistdir.index(expected_fname_noex)]
                    )

                    # print("Resolved %s to %s" % (self.expected_filename, lfile))
                    lsize = os.path.getsize(lfile)
                    # print("Remote size: %s Local size: %s" % (rsize, lsize))

                    if lsize != rsize:
                        await self._really_download(hash=True)
                    else:
                        # print("[Download] Cached:", self.url)
                        self.filename = lfile

                else:
                    # print("File not found in cache (%s)" % expected_fname_noex)
                    await self._really_download(hash=True)

            else:
                ldir = os.listdir(self.download_folder)
                flistdir = [f.rsplit('.', 1)[0] for f in ldir]
                expected_fname_base = os.path.basename(self.expected_filename)
                expected_fname_noex = expected_fname_base.rsplit('.', 1)[0]

                # idk wtf this is but its probably legacy code
                # or i have youtube to blame for changing shit again

                if expected_fname_base in ldir:
                    self.filename = os.path.join(self.download_folder, expected_fname_base)
                    log.info("Download cached: {}".format(self.url))

                elif expected_fname_noex in flistdir:
                    log.info("Download cached (different extension): {}".format(self.url))
                    self.filename = os.path.join(self.download_folder, ldir[flistdir.index(expected_fname_noex)])
                    log.debug("Expected {}, got {}".format(
                        self.expected_filename.rsplit('.', 1)[-1],
                        self.filename.rsplit('.', 1)[-1]
                    ))
                else:
                    await self._really_download()

            # Trigger ready callbacks.
            self._for_each_future(lambda future: future.set_result(self))

        except Exception as e:
            traceback.print_exc()
            self._for_each_future(lambda future: future.set_exception(e))

        finally:
            self._is_downloading = False

    # noinspection PyShadowingBuiltins
    async def _really_download(self, *, hash=False):
        log.info("Download started: {}".format(self.url))

        try:
            result = await self.playlist.downloader.extract_info(self.playlist.loop, self.url, download=True)
        except Exception as e:
            raise ExtractionError(e)

        log.info("Download complete: {}".format(self.url))

        if result is None:
            log.critical("YTDL has failed, everyone panic")
            raise ExtractionError("ytdl broke and hell if I know why")
            # What the fuck do I do now?

        self.filename = unhashed_fname = self.playlist.downloader.ytdl.prepare_filename(result)

        if hash:
            # insert the 8 last characters of the file hash to the file name to ensure uniqueness
            self.filename = md5sum(unhashed_fname, 8).join('-.').join(unhashed_fname.rsplit('.', 1))

            if os.path.isfile(self.filename):
                # Oh bother it was actually there.
                os.unlink(unhashed_fname)
            else:
                # Move the temporary file to it's final location.
                os.rename(unhashed_fname, self.filename)


class StreamPlaylistEntry(BasePlaylistEntry):
    def __init__(self, playlist, url, title, *, direct=False, source_url=None, **meta):
        super().__init__()

        self.playlist = playlist
        self._url = url
        self.source_url = source_url
        self.title = title
        self.direct = direct
        self.duration = 0
        self.meta = meta

        if self.direct:
            self.filename = self._url

    @property
    def url(self):
        return self.source_url or self._url

    @classmethod
    def _deserialize(cls, data, playlist=None):
        assert playlist is not None, cls._bad('playlist')

        try:
            # TODO: version check
            url = data['url']
            title = data['title']
            direct = data['direct']
            meta = {}

            # TODO: Better [name] fallbacks
            if 'channel' in data['meta']:
                ch = playlist.bot.get_channel(data['meta']['channel']['id'])
                meta['channel'] = ch or data['meta']['channel']['name']

            if 'author' in data['meta']:
                meta['author'] = meta['channel'].server.get_member(data['meta']['author']['id'])

            return cls(playlist, url, title, direct=direct, **meta)
        except Exception as e:
            log.error("Could not load {}".format(cls.__name__), exc_info=e)

    def __json__(self):
        return self._enclose_json({
            'version': 1,
            'url': self._url,
            'title': self.title,
            'direct': self.direct,
            'meta': {
                name: {
                    'type': obj.__class__.__name__,
                    'id': obj.id,
                    'name': obj.name
                } for name, obj in self.meta.items()
            }
            # Actually I think I can just getattr(discord, type)
        })

    async def _download(self):
        self._is_downloading = True

        try:
            result = await self.playlist.downloader.extract_info(self.playlist.loop, self._url, download=False)
        except Exception as e:
            raise ExtractionError(e)
        else:
            self.filename = result['url']
            # I might need some sort of events or hooks or shit
            # for when ffmpeg inevitebly fucks up and i have to restart
            # although maybe that should be at a slightly lower level
        finally:
            self._is_downloading = False

