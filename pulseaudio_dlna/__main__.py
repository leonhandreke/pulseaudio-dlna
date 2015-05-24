#!/usr/bin/python

# This file is part of pulseaudio-dlna.

# pulseaudio-dlna is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# pulseaudio-dlna is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with pulseaudio-dlna.  If not, see <http://www.gnu.org/licenses/>.

'''
Usage:
    pulseaudio-dlna [--host <host>] [--port <port>] [--encoder <encoder>] [--bit-rate=<rate>] [--filter-device=<filter-device>] [--renderer-urls <urls>] [--debug]
    pulseaudio-dlna [-h | --help | --version]

Options:
       --host=<host>                       Set the server ip.
    -p --port=<port>                       Set the server port [default: 8080].
    -e --encoder=<encoder>                 Set the audio encoder.
                                           Possible encoders are:
                                             - mp3   MPEG Audio Layer III (MP3)
                                             - ogg   Ogg Vorbis
                                             - flac  Free Lossless Audio Codec (FLAC)
                                             - wav   Waveform Audio File Format (WAV)
                                             - opus  Opus Interactive Audio Codec (OPUS)
    -b --bit-rate=<rate>                   Set the audio encoder's bitrate.
    --filter-device=<filter-device>        Set a name filter for devices which should be added.
                                           Devices which get discovered, but won't match the
                                           filter text will be skipped.
    --renderer-urls=<urls>                 Set the renderer urls yourself. no discovery will commence.
    --debug                                enables detailed debug messages.
    -v --version                           Show the version.
    -h --help                              Show the help.
'''

from __future__ import unicode_literals

import dbus
import dbus.mainloop.glib
import multiprocessing
import setproctitle
import logging
import sys
import signal
import socket
import docopt

import pulseaudio_dlna
import pulseaudio_dlna.common
from pulseaudio_dlna.discover import RendererDiscover
from pulseaudio_dlna.listener import ThreadedSSDPListener
import pulseaudio_dlna.plugins.upnp
import pulseaudio_dlna.plugins.chromecast
import pulseaudio_dlna.encoders
from pulseaudio_dlna.renderers import RendererHolder
import pulseaudio_dlna.streamserver
import pulseaudio_dlna.pulseaudio
import pulseaudio_dlna.utils.network


class PulseAudioDLNA(object):
    def __init__(self):
        self.server_process = None
        self.pulse = None

    def shutdown(self, signal_number=None, frame=None):
        print('Application is shutting down.')
        if self.pulse is not None:
            self.pulse.cleanup()
        if self.server_process is not None:
            self.server_process.server_close()
        sys.exit(1)

    def startup(self):
        options = docopt.docopt(__doc__, version=pulseaudio_dlna.__version__)

        level = logging.DEBUG
        if not options['--debug']:
            level = logging.INFO
            logging.getLogger('requests').setLevel(logging.WARNING)
            logging.getLogger('urllib3').setLevel(logging.WARNING)

        logging.basicConfig(
            level=level,
            format='%(asctime)s %(name)-46s %(levelname)-8s %(message)s',
            datefmt='%m-%d %H:%M:%S')
        logger = logging.getLogger('pulseaudio_dlna')

        if not options['--host']:
            host = pulseaudio_dlna.utils.network.default_ipv4()
            if host is None:
                logger.info(
                    'I could not determine your host address. '
                    'You must specify it yourself via the --host option!')
                sys.exit(1)
        else:
            host = str(options['--host'])

        port = int(options['--port'])

        logger.info('Using localhost: {host}:{port}'.format(
            host=host, port=port))

        plugins = [
            pulseaudio_dlna.plugins.upnp.DLNAPlugin(),
            pulseaudio_dlna.plugins.chromecast.ChromecastPlugin(),
        ]

        if options['--encoder']:
            for encoder in pulseaudio_dlna.common.supported_encoders:
                if encoder.suffix == options['--encoder']:
                    pulseaudio_dlna.common.supported_encoders = [encoder]
                    break
            if len(pulseaudio_dlna.common.supported_encoders) != 1:
                logger.error('You specified an unknown encoder! '
                             'Application terminates.')
                sys.exit(1)

        if options['--bit-rate']:
            for encoder in pulseaudio_dlna.common.supported_encoders:
                try:
                    encoder.bit_rate = options['--bit-rate']
                except pulseaudio_dlna.encoders.UnsupportedBitrateException:
                    if len(encoder.bit_rates) > 0:
                        logger.error(
                            'You specified an invalid bit rate '
                            'for the encoder! Supported bit rates '
                            'are "{bit_rates}"! '
                            'Application terminates.'.format(
                                bit_rates=','.join(
                                    str(e) for e in encoder.bit_rates)))
                    else:
                        logger.error('You selected encoder does not support '
                                     'setting a specific bit rate! '
                                     'Application terminates.')
                    sys.exit(1)

        logger.info('Loaded encoders:')
        for encoder in pulseaudio_dlna.common.supported_encoders:
            encoder.validate()
            print(encoder)

        manager = multiprocessing.Manager()
        message_queue = multiprocessing.Queue()
        bridges = manager.list()

        try:
            self.stream_server = pulseaudio_dlna.streamserver.ThreadedStreamServer(
                host, port, bridges, message_queue)
        except socket.error:
            logger.info(
                'The streaming server could not bind to your specified port '
                '({port}). Perhaps this is already in use? Application '
                'terminates.'.format(port=port))
            sys.exit(1)

        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        pulse = pulseaudio_dlna.pulseaudio.PulseWatcher(bridges, message_queue)

        device_filter = None
        if options['--filter-device']:
            device_filter = options['--filter-device'].split(',')
        renderer_holder = RendererHolder(self.stream_server, message_queue, plugins, device_filter)

        self.ssdp_listener = ThreadedSSDPListener(renderer_holder)

        pulse_process = multiprocessing.Process(target=self.pulse.run)
        server_process = multiprocessing.Process(target=self.stream_server.run)
        listener_process = multiprocessing.Process(target=self.ssdp_listener.run)
        pulse_process.start()
        server_process.start()
        listener_process.start()

        if options['--renderer-urls']:
            locations = options['--renderer-urls'].split(',')
            renderer_holder.add_renderers_by_url(locations)
        else:
            discover = RendererDiscover(renderer_holder)
            discover.search()
            logger.info('Discovery complete.')

        setproctitle.setproctitle('pulseaudio-dlna')
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)

        pulse_process.join()
        server_process.join()
        listener_process.join()


def main(argv=sys.argv[1:]):
    pulseaudio_dlna = PulseAudioDLNA()
    pulseaudio_dlna.startup()


if __name__ == "__main__":
    sys.exit(main())
