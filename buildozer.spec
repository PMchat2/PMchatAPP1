[app]
title = 隐私通讯程式
package.name = privatebinchat
package.domain = org.privatebin
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,pem
version = 0.1

# ---- 必须列全依赖项，否则构建会失败 ----
requirements = python3==3.11.9,hostpython3==3.11.9,kivy==2.3.1,requests,urllib3,chardet,idna,certifi,pycryptodome

# ---- Android 权限 ----
android.permissions = INTERNET, ACCESS_NETWORK_STATE

# ---- Android 编译参数 ----
android.api = 30
android.minapi = 21
android.ndk = 23b
android.arch = arm64-v8a, armeabi-v7a
android.allow_backup = True
android.accept_sdk_license = True

# ---- 日志 ----
log_level = 2

# ---- 屏幕方向（手机一般锁竖屏）----
orientation = portrait