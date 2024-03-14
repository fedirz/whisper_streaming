#!/usr/bin/env python3
import argparse
import io
import logging
import socket
import time

import librosa
import numpy as np
import soundfile

import line_packet
from whisper_online import (SAMPLING_RATE, FasterWhisperASR,
                            OnlineASRProcessor, add_shared_args)

LOG_LEVEL = logging.INFO
PACKET_SIZE = 65536

parser = argparse.ArgumentParser()

# server options
parser.add_argument("--host", type=str, default="localhost")
parser.add_argument("--port", type=int, default=43007)


# options from whisper_online
add_shared_args(parser)
args = parser.parse_args()


# setting whisper object by args

model_size = args.model_size

t = time.time()
print(f"Loading Whisper {model_size} model...")


asr = FasterWhisperASR(
    model_size=model_size,
    cache_dir=args.model_cache_dir,
    model_dir=args.model_dir,
)

e = time.time()
print(f"done. It took {round(e-t,2)} seconds.")


min_chunk = args.min_chunk_size

######### Server objects


class Connection:
    """it wraps conn object"""

    def __init__(self, conn):
        self.conn = conn
        self.last_line = ""

        self.conn.setblocking(True)

    def send(self, line):
        """it doesn't send the same line twice, because it was problematic in online-text-flow-events"""
        if line == self.last_line:
            return
        line_packet.send_one_line(self.conn, line)
        self.last_line = line

    def receive_lines(self):
        in_line = line_packet.receive_lines(self.conn)
        return in_line

    def non_blocking_receive_audio(self):
        r = self.conn.recv(PACKET_SIZE)
        return r


# wraps socket and ASR object, and serves one client connection.
# next client should be served by a new instance of this object
class ServerProcessor:
    def __init__(self, c: Connection, min_chunk: float):
        self.connection = c
        self.min_chunk = min_chunk

        self.last_end = None

    def receive_audio_chunk(self):
        # receive all audio that is available by this time
        # blocks operation if less than self.min_chunk seconds is available
        # unblocks if connection is closed or a chunk is available
        out = []
        while sum(len(x) for x in out) < self.min_chunk * SAMPLING_RATE:
            raw_bytes = self.connection.non_blocking_receive_audio()
            print(raw_bytes[:10])
            print(len(raw_bytes))
            if not raw_bytes:
                break
            sf = soundfile.SoundFile(
                io.BytesIO(raw_bytes),
                channels=1,
                endian="LITTLE",
                samplerate=SAMPLING_RATE,
                subtype="PCM_16",
                format="RAW",
            )
            audio, _ = librosa.load(sf, sr=SAMPLING_RATE, dtype=np.float32)
            out.append(audio)
        if not out:
            return None
        return np.concatenate(out)

    def format_output_transcript(self, o):
        # output format in stdout is like:
        # 0 1720 Takhle to je
        # - the first two words are:
        #    - beg and end timestamp of the text segment, as estimated by Whisper model. The timestamps are not accurate, but they're useful anyway
        # - the next words: segment transcript

        # This function differs from whisper_online.output_transcript in the following:
        # succeeding [beg,end] intervals are not overlapping because ELITR protocol (implemented in online-text-flow events) requires it.
        # Therefore, beg, is max of previous end and current beg outputed by Whisper.
        # Usually it differs negligibly, by appx 20 ms.

        if o[0] is not None:
            beg, end = o[0] * 1000, o[1] * 1000
            if self.last_end is not None:
                beg = max(beg, self.last_end)

            self.last_end = end
            print("%1.0f %1.0f %s" % (beg, end, o[2]))
            return "%1.0f %1.0f %s" % (beg, end, o[2])
        else:
            print(o)
            return None

    def send_result(self, o):
        msg = self.format_output_transcript(o)
        if msg is not None:
            self.connection.send(msg)

    def process(self):
        # handle one client connection
        online_asr = OnlineASRProcessor(
            asr, buffer_trimming_sec=args.buffer_trimming_sec
        )
        while True:
            a = self.receive_audio_chunk()
            if a is None:
                print("break here")
                break
            online_asr.insert_audio_chunk(a)
            o = online_asr.process_iter()
            try:
                self.send_result(o)
            except BrokenPipeError:
                print("broken pipe -- connection closed?")
                break


#        o = online.finish()  # this should be working
#        self.send_result(o)


logging.basicConfig(level=LOG_LEVEL, format="whisper-server-%(levelname)s: %(message)s")

# server loop

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((args.host, args.port))
    s.listen(1)
    logging.info("INFO: Listening on" + str((args.host, args.port)))
    while True:
        conn, addr = s.accept()
        logging.info("INFO: Connected to client on {}".format(addr))
        connection = Connection(conn)
        proc = ServerProcessor(connection, min_chunk)
        proc.process()
        conn.close()
        logging.info("INFO: Connection to client closed")
logging.info("INFO: Connection closed, terminating.")
