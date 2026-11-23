# PiCam Android app

Full-screen viewer for the PiCam stream on a Raspberry Pi. When it opens, it
looks for the Pi in this order: the last address that worked (192.168.0.33 by
default), then picam.local, then the PiCam hotspot (10.42.0.1). The VR and
Settings buttons come from the Pi's own pages.

## Build option A: GitHub (no software to install)
1. Create a free GitHub account and a new **private** repository.
2. Upload everything in this folder, including the `.github` and `gradle`
   folders. In the browser, use "Add file > Upload files" and drag the
   folder's contents in.
3. Open the **Actions** tab and wait about 5 minutes for "Build PiCam APK"
   to turn green.
4. Open the run and download **PiCam-apk** at the bottom. Unzip it to get
   `PiCam.apk`.

## Build option B: Android Studio (Windows)
1. Install Android Studio and open this folder.
2. Wait for the Gradle sync to finish.
3. Go to Build > Build App Bundle(s) / APK(s) > Build APK(s).
4. The APK is saved to `app/build/outputs/apk/`.

## Installing on the Galaxy Tab A
Copy `PiCam.apk` to the tablet and tap it. When Android asks, allow
"Install unknown apps" for your file manager or browser.
