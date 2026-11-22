[app]
title = Tank Controller
package.name = tankcontroller
package.domain = org.example

source.dir = .
source.include_exts = py,png,jpg,kv,atlas
source.exclude_dirs = bin

version = 1.3

# kivy version is intentionally NOT pinned (p4a picks the compatible one)
requirements = python3,kivy,requests,pyjnius

orientation = landscape
fullscreen = 1

android.permissions = INTERNET,ACCESS_NETWORK_STATE,RECORD_AUDIO
# <queries> block so the speech recognizer service can be found (Android 11+)
android.extra_manifest_xml = ./extra_manifest.xml
# allow plain http (the ESP robot only speaks http)
android.extra_manifest_application_arguments = ./application-attributes.xml

android.api = 36
android.minapi = 24
android.ndk = 28c
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True

# python-for-android develop: the only branch that supports Python 3.14 (see Buildozer docs)
p4a.branch = develop

[buildozer]
log_level = 2
warn_on_root = 0
