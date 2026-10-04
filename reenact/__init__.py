"""Hypit-style reenactment: understand a reference, write a new Script, direct new
material, align it to the words and compose a finished, packaged video.

Stages (each persisted as a project node so a restart resumes instead of repaying):
  analysis  -> reference understanding (ASR words + Qwen-VL reading)
  direction -> Brief, Treatment, casting, camera setups (DeepSeek)
  script    -> new Script with Moments/Selections + packaging plan (DeepSeek)
  images    -> camera-setup pictures, icons, B-roll (Image2, QA + retry)
  voices    -> per-speaker voice-timbre references from the source audio
  takes     -> one H3 Ref2VA performance per Segment (concurrent, QA + retry)
  render    -> word alignment, captions, MG components, sound, final encode
"""
