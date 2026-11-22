[app]
title = Tank Controller
package.name = tankcontroller
package.domain = org.example

source.dir = .
source.include_exts = py,png,jpg,kv,atlas
source.exclude_dirs = bin

version = 1.3

# plyer حذف شد (در کد استفاده نمی‌شود)
requirements = python3,kivy==2.2.1,requests,urllib3,idna,charset-normalizer,certifi,pyjnius

orientation = landscape
fullscreen = 1

android.permissions = INTERNET,ACCESS_NETWORK_STATE,RECORD_AUDIO
# بخش <queries> برای پیدا کردن سرویس تشخیص گفتار (اندروید ۱۱+)
android.extra_manifest_xml = ./extra_manifest.xml
# اجازه‌ی HTTP ساده (ربات ESP فقط http دارد؛ بدون این خط اندروید ارتباط را می‌بندد)
android.extra_manifest_application_arguments = ./application-attributes.xml

android.api = 33
android.minapi = 24
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 0
