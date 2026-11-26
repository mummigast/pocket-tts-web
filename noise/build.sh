#!/bin/bash
# Builds noise/*.wav (90 s, mono 24 kHz, -30 LUFS) from the Wikimedia Commons downloads in noise/src/.
# Sources + licences: noise/credits.json. Re-download with the titles listed there.
set -e; cd "$(dirname "$0")"
mk() { # id source start
  ffmpeg -v error -y -ss "$3" -t 90 -i "src/$2" -ac 1 -ar 24000 -af "highpass=f=40,loudnorm=I=-30:TP=-6:LRA=11" -c:a pcm_s16le "$1.wav"
  echo "built $1"; }
mk street   "Karlova 0001.ogg" 20
mk rain     "Urban Street on a Rainy Afternoon.flac" 120
mk cafe     "Restaurant ambience.ogg" 3
mk mall     "1 minute at the alexa mall in berlin.ogg" 0
mk office   "Ambient classroom mono.ogg" 0
mk station  "WWS TheStationTunnelOfTheTampereStation.ogg" 10
mk subway   "TTC Subway Line 1 Ambience - Dupont to St George (Freesound).ogg" 20
mk birds    "Gentle breeze and birds singing.ogg" 0
ffmpeg -v error -y -f lavfi -i "anoisesrc=c=pink:r=24000:a=0.5:d=90" -af "lowpass=f=6000,loudnorm=I=-30:TP=-6" -ac 1 -c:a pcm_s16le roomtone.wav
echo "built roomtone"
