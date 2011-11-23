# -*- coding: utf-8 -*-
# Copyright: Damien Elmes <anki@ichi2.net>
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import re, sys, threading, time, subprocess, os, signal, errno, atexit
import shutil
from anki.hooks import addHook, runHook
from anki.utils import namedtmp, tmpdir, isWin, isMac

# Shared utils
##########################################################################

def playFromText(text):
    for match in re.findall("\[sound:(.*?)\]", text):
        play(match)

def stripSounds(text):
    return re.sub("\[sound:.*?\]", "", text)

def hasSound(text):
    return re.search("\[sound:.*?\]", text) is not None

##########################################################################

processingSrc = "rec.wav"
processingDst = "rec.mp3"
processingChain = []
recFiles = []

processingChain = [
    ["lame", "rec.wav", processingDst, "--noreplaygain", "--quiet"],
    ]

# don't show box on windows
if isWin:
    si = subprocess.STARTUPINFO()
    try:
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    except:
        # python2.7+
        si.dwFlags |= subprocess._subprocess.STARTF_USESHOWWINDOW
else:
    si = None

if isMac:
    # make sure lame, which is installed in /usr/local/bin, is in the path
    os.environ['PATH'] += ":" + "/usr/local/bin"
    dir = os.path.dirname(os.path.abspath(__file__))
    dir = os.path.abspath(dir + "/../../../..")
    os.environ['PATH'] += ":" + dir + "/audio"

def retryWait(proc):
    # osx throws interrupted system call errors frequently
    while 1:
        try:
            return proc.wait()
        except OSError:
            continue

# Mplayer settings
##########################################################################

if isWin:
    mplayerCmd = ["mplayer.exe", "-ao", "win32"]
    dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    os.environ['PATH'] += ";" + dir
    os.environ['PATH'] += ";" + dir + "\\..\\win\\top" # for testing
else:
    mplayerCmd = ["mplayer"]
mplayerCmd += ["-really-quiet", "-noautosub"]

# Mplayer in slave mode
##########################################################################

mplayerQueue = []
mplayerManager = None
mplayerReader = None
mplayerEvt = threading.Event()
mplayerClear = False

# fixme from robert: can we do away with this with stderr=file(os.devnull,
# 'w') in the popen call?
class MplayerReader(threading.Thread):
    "Read any debugging info to prevent mplayer from blocking."

    def run(self):
        while 1:
            mplayerEvt.wait()
            try:
                mplayerManager.mplayer.stdout.read()
            except:
                pass

class MplayerMonitor(threading.Thread):

    def run(self):
        global mplayerClear
        self.mplayer = None
        self.deadPlayers = []
        while 1:
            mplayerEvt.wait()
            if mplayerQueue:
                # ensure started
                if not self.mplayer:
                    self.startProcess()
                # loop through files to play
                while mplayerQueue:
                    item = mplayerQueue.pop(0)
                    if mplayerClear:
                        mplayerClear = False
                        extra = ""
                    else:
                        extra = " 1"
                    cmd = 'loadfile "%s"%s\n' % (item, extra)
                    try:
                        self.mplayer.stdin.write(cmd)
                    except:
                        # mplayer has quit and needs restarting
                        self.deadPlayers.append(self.mplayer)
                        self.mplayer = None
                        self.startProcess()
                        self.mplayer.stdin.write(cmd)
            # wait() on finished processes. we don't want to block on the
            # wait, so we keep trying each time we're reactivated
            def clean(pl):
                if pl.poll() is not None:
                    pl.wait()
                    return False
                else:
                    return True
            self.deadPlayers = [pl for pl in self.deadPlayers if clean(pl)]
            mplayerEvt.clear()

    def kill(self):
        if not self.mplayer:
            return
        try:
            self.mplayer.stdin.write("quit\n")
            self.deadPlayers.append(self.mplayer)
        except:
            pass
        self.mplayer = None

    def startProcess(self):
        try:
            cmd = mplayerCmd + ["-slave", "-idle"]
            self.mplayer = subprocess.Popen(
                cmd, startupinfo=si, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError:
            mplayerEvt.clear()
            raise Exception("Audio player not found")

def queueMplayer(path):
    ensureMplayerThreads()
    while mplayerEvt.isSet():
        time.sleep(0.1)
    if isWin and os.path.exists(path):
        # mplayer on windows doesn't like the encoding, so we create a
        # temporary file instead. oddly, foreign characters in the dirname
        # don't seem to matter.
        dir = tmpdir().encode(sys.getfilesystemencoding())
        name = os.path.join(dir, "audio"+os.path.splitext(path)[1])
        f = open(name, "wb")
        f.write(open(path, "rb").read())
        f.close()
        # it wants unix paths, too!
        path = name.replace("\\", "/")
        path = path.encode(sys.getfilesystemencoding())
    else:
        path = path.encode("utf-8")
    mplayerQueue.append(path)
    mplayerEvt.set()

def clearMplayerQueue():
    global mplayerClear
    mplayerClear = True
    mplayerEvt.set()

def ensureMplayerThreads():
    global mplayerManager, mplayerReader
    if not mplayerManager:
        mplayerManager = MplayerMonitor()
        mplayerManager.daemon = True
        mplayerManager.start()
        mplayerReader = MplayerReader()
        mplayerReader.daemon = True
        mplayerReader.start()

def stopMplayer(*args):
    if not mplayerManager:
        return
    mplayerManager.kill()

addHook("colClosed", stopMplayer)

# PyAudio recording
##########################################################################

try:
    import pyaudio
    import wave

    PYAU_FORMAT = pyaudio.paInt16
    PYAU_CHANNELS = 1
    PYAU_RATE = 44100
    PYAU_INPUT_INDEX = None
except:
    pass

class _Recorder(object):

    def postprocess(self, encode=True):
        self.encode = encode
        for c in processingChain:
            #print c
            if not self.encode and c[0] == 'lame':
                continue
            try:
                ret = retryWait(subprocess.Popen(c, startupinfo=si))
            except:
                ret = True
            if ret:
                raise Exception(_(
                    "Error running %s") %
                                u" ".join(c))

class PyAudioThreadedRecorder(threading.Thread):

    def __init__(self):
        threading.Thread.__init__(self)
        self.finish = False

    def run(self):
        chunk = 1024
        try:
            p = pyaudio.PyAudio()
        except NameError:
            raise Exception(
                "Pyaudio not installed (recording not supported on OSX10.3)")
        stream = p.open(format=PYAU_FORMAT,
                        channels=PYAU_CHANNELS,
                        rate=PYAU_RATE,
                        input=True,
                        input_device_index=PYAU_INPUT_INDEX,
                        frames_per_buffer=chunk)
        all = []
        while not self.finish:
            try:
                data = stream.read(chunk)
            except IOError, e:
                if e[1] == pyaudio.paInputOverflowed:
                    data = None
                else:
                    raise
            if data:
                all.append(data)
        stream.close()
        p.terminate()
        data = ''.join(all)
        wf = wave.open(processingSrc, 'wb')
        wf.setnchannels(PYAU_CHANNELS)
        wf.setsampwidth(p.get_sample_size(PYAU_FORMAT))
        wf.setframerate(PYAU_RATE)
        wf.writeframes(data)
        wf.close()

class PyAudioRecorder(_Recorder):

    def __init__(self):
        for t in recFiles + [processingSrc, processingDst]:
            try:
                os.unlink(t)
            except OSError:
                pass
        self.encode = False

    def start(self):
        self.thread = PyAudioThreadedRecorder()
        self.thread.start()

    def stop(self):
        self.thread.finish = True
        self.thread.join()

    def file(self):
        if self.encode:
            tgt = "rec%d.mp3" % time.time()
            os.rename(processingDst, tgt)
            return tgt
        else:
            return recFiles[1]

# Audio interface
##########################################################################

_player = queueMplayer
_queueEraser = clearMplayerQueue

def play(path):
    _player(path)

def clearAudioQueue():
    _queueEraser()

Recorder = PyAudioRecorder
