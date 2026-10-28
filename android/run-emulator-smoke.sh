#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SDK="${ANDROID_HOME:?ANDROID_HOME must point to the Android SDK}"
REPORTS="$PROJECT_ROOT/android/app/build/reports"
mkdir -p "$REPORTS"
TOOLS="$SDK/cmdline-tools/latest/bin"
if [[ ! -x "$TOOLS/android" && ! -x "$TOOLS/sdkmanager" ]]; then
  TOOLS=""
  for candidate in "$SDK"/cmdline-tools/*/bin; do
    if [[ -x "$candidate/android" || -x "$candidate/sdkmanager" ]]; then
      TOOLS="$candidate"
      break
    fi
  done
fi
if [[ -z "$TOOLS" ]]; then
  echo "Android command-line tools were not found under $SDK." >&2
  exit 1
fi
if [[ -x "$TOOLS/android" ]]; then
  "$TOOLS/android" --no-metrics --sdk="$SDK" sdk install emulator system-images/android-35/google_apis/x86_64
else
  "$TOOLS/sdkmanager" --sdk_root="$SDK" "emulator" "system-images;android-35;google_apis;x86_64"
fi
if [[ ! -e /dev/kvm ]]; then
  echo "The Android emulator smoke test requires the runner's KVM device." >&2
  exit 1
fi
sudo chmod 666 /dev/kvm
AVDMANAGER="$TOOLS/avdmanager"
# Older avdmanager and newer emulator releases use different default home layouts.
# Keep both tools on the same explicit directory in this ephemeral runner.
export ANDROID_USER_HOME="${RUNNER_TEMP:-$PROJECT_ROOT/android/build}/kmlink-android-home"
export ANDROID_EMULATOR_HOME="$ANDROID_USER_HOME"
export ANDROID_AVD_HOME="$ANDROID_USER_HOME/avd"
mkdir -p "$ANDROID_AVD_HOME"
printf 'no\n' | "$AVDMANAGER" create avd --force --name kmlink-smoke --path "$ANDROID_AVD_HOME/kmlink-smoke.avd" --package "system-images;android-35;google_apis;x86_64" --device pixel_5
if ! "$SDK/emulator/emulator" -list-avds | grep -Fx 'kmlink-smoke'; then
  echo "The emulator cannot find the AVD created in $ANDROID_AVD_HOME." >&2
  exit 1
fi
export ANDROID_SERIAL=emulator-5554
"$SDK/platform-tools/adb" start-server
"$SDK/emulator/emulator" -avd kmlink-smoke -port 5554 -no-window -no-audio -no-snapshot -no-boot-anim -gpu swiftshader_indirect -accel on -cores 2 -memory 2048 > "$REPORTS/emulator.log" 2>&1 &
EMULATOR_PID=$!
cleanup() {
  # Preserve runtime evidence even when Gradle returns a test failure.
  timeout 10 "$SDK/platform-tools/adb" -s "$ANDROID_SERIAL" logcat -d -v threadtime > "$REPORTS/logcat.txt" 2>&1 || true
  timeout 10 "$SDK/platform-tools/adb" -s "$ANDROID_SERIAL" pull /sdcard/Android/data/io.kmlink.android/files/qa "$REPORTS/qa" >/dev/null 2>&1 || true
  "$SDK/platform-tools/adb" -s "$ANDROID_SERIAL" emu kill >/dev/null 2>&1 || true
  kill "$EMULATOR_PID" >/dev/null 2>&1 || true
}
trap cleanup EXIT
BOOTED=0
DEADLINE=$((SECONDS + 120))
while (( SECONDS < DEADLINE )); do
  if [[ "$(timeout 5 "$SDK/platform-tools/adb" -s "$ANDROID_SERIAL" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" == "1" ]]; then
    BOOTED=1
    break
  fi
  if ! kill -0 "$EMULATOR_PID" 2>/dev/null; then
    tail -n 100 "$REPORTS/emulator.log"
    exit 1
  fi
  sleep 2
done
if [[ "$BOOTED" != "1" ]]; then
  tail -n 100 "$REPORTS/emulator.log"
  echo "Android emulator did not boot within the bounded wait." >&2
  exit 1
fi
"$SDK/platform-tools/adb" -s "$ANDROID_SERIAL" shell input keyevent 82
cd "$PROJECT_ROOT/android"
./gradlew --no-daemon :app:connectedDebugAndroidTest
"$SDK/platform-tools/adb" -s "$ANDROID_SERIAL" shell am start -W -n io.kmlink.android/.MainActivity >/dev/null
"$SDK/platform-tools/adb" -s "$ANDROID_SERIAL" exec-out screencap -p > "$REPORTS/android-smoke.png"
