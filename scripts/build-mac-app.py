"""Build a local permission-owning app. Does not launch it or grant access."""
from pathlib import Path
import plistlib
import subprocess

root=Path(__file__).resolve().parents[1]
app=root/'.runtime/Dot Phone Call Helper.app'
contents=app/'Contents'
binary=contents/'MacOS/DotPhone'
binary.parent.mkdir(parents=True,exist_ok=True)
info={'CFBundleIdentifier':'dev.dot-phone-call.helper',
      'CFBundleName':'Dot Phone Call Helper','CFBundleDisplayName':'Dot Phone Call Helper',
      'CFBundleExecutable':'DotPhone','CFBundlePackageType':'APPL',
      'CFBundleShortVersionString':'0.1.0','CFBundleVersion':'1',
      'LSUIElement':True,'LSMinimumSystemVersion':'13.0',
      'NSMicrophoneUsageDescription':'Dot Phone captures BlackHole virtual audio to carry Dot’s voice to your phone. The bridge selects the BlackHole devices, not your physical microphone.',
      'DotPhoneProjectRoot':str(root)}
(contents/'Info.plist').write_bytes(plistlib.dumps(info))
entitlements=root/'.runtime/mac-app-entitlements.plist'
entitlements.write_bytes(plistlib.dumps({'com.apple.security.device.audio-input':True}))
subprocess.run(['swiftc','-module-cache-path',str(root/'.runtime/swift-cache'),
                str(root/'mac-bridge/launcher.swift'),'-o',str(binary),
                '-framework','AppKit','-framework','AVFoundation'],check=True)
subprocess.run(['codesign','--force','--sign','-','--options','runtime',
                '--entitlements',str(entitlements),str(app)],check=True)
subprocess.run(['codesign','--verify','--strict',str(app)],check=True)
print('Built and verified:',app)
