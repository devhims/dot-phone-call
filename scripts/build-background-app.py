"""Build the named menu-bar app. Does not change installed services."""
from pathlib import Path
import plistlib
import subprocess
root=Path(__file__).resolve().parents[1]
app=root/'.runtime/Dot Phone.app'
contents=app/'Contents';binary=contents/'MacOS/DotPhone'
binary.parent.mkdir(parents=True,exist_ok=True)
info={'CFBundleIdentifier':'dev.dot-phone-call.app','CFBundleName':'Dot Phone',
      'CFBundleDisplayName':'Dot Phone','CFBundleExecutable':'DotPhone','CFBundlePackageType':'APPL',
      'CFBundleShortVersionString':'0.1.0','CFBundleVersion':'1','LSUIElement':True,
      'LSMinimumSystemVersion':'13.0','DotPhoneProjectRoot':str(root)}
(contents/'Info.plist').write_bytes(plistlib.dumps(info))
subprocess.run(['swiftc','-module-cache-path',str(root/'.runtime/swift-cache'),str(root/'mac-app/App.swift'),'-o',str(binary),'-framework','AppKit'],check=True)
subprocess.run(['codesign','--force','--sign','-','--options','runtime',str(app)],check=True)
subprocess.run(['codesign','--verify','--strict',str(app)],check=True)
print('Built and verified:',app)
