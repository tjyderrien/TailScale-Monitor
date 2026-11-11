#!/bin/sh
# Build the graphical macOS installer, macos/build/tailmon-<version>.pkg. Run on a Mac (needs pkgbuild and
# productbuild, which come with macOS).
#
#   macos/build-pkg.sh                       unsigned package, version = today's date
#   VERSION=1.2 macos/build-pkg.sh
#   SIGN_ID="Developer ID Installer: Name (TEAMID)" macos/build-pkg.sh             signed
#   SIGN_ID=... NOTARY_PROFILE=tailmon macos/build-pkg.sh                           signed + notarized
#     (create the profile once: xcrun notarytool store-credentials tailmon)
set -eu

here=$(cd "$(dirname "$0")" && pwd)
repo=$(dirname "$here")
build="$here/build"
version=${VERSION:-$(date +%Y.%m.%d)}
id=io.github.tjyderrien.tailmon

for tool in pkgbuild productbuild; do
    command -v "$tool" >/dev/null || { echo "$tool not found: run this on macOS" >&2; exit 1; }
done

rm -rf "$build"
mkdir -p "$build/root/usr/local/bin" "$build/root/usr/local/share/man/man1" "$build/root/usr/local/share/tailmon" \
         "$build/root/usr/local/share/zsh/site-functions" \
         "$build/resources" "$build/empty"

# Payload: the script, its man page, its zsh completion, the licence and an uninstaller.
install -m 755 "$repo/tailmon.py" "$build/root/usr/local/bin/tailmon"
install -m 644 "$repo/tailmon.1" "$build/root/usr/local/share/man/man1/tailmon.1"
python3 "$repo/tailmon.py" completion zsh > "$build/root/usr/local/share/zsh/site-functions/_tailmon"
chmod 644 "$build/root/usr/local/share/zsh/site-functions/_tailmon"
install -m 644 "$repo/LICENSE" "$build/root/usr/local/share/tailmon/LICENSE"
install -m 755 "$here/uninstall.sh" "$build/root/usr/local/share/tailmon/uninstall.sh"
chmod 755 "$here/scripts/core/postinstall" "$here/scripts/tailscale-cli/postinstall"

pkgbuild --root "$build/root" --identifier "$id" --version "$version" --ownership recommended \
         --scripts "$here/scripts/core" --install-location / "$build/tailmon-core.pkg"
pkgbuild --nopayload --identifier "$id.tailscale-cli" --version "$version" \
         --scripts "$here/scripts/tailscale-cli" "$build/tailscale-cli.pkg"

# Screens shown by Installer.app.
cp "$here/resources/welcome.html" "$here/resources/conclusion.html" "$build/resources/"
cp "$repo/LICENSE" "$build/resources/LICENSE.txt"
sed "s/@VERSION@/$version/g" "$here/distribution.xml" > "$build/distribution.xml"

out="$build/tailmon-$version.pkg"
set -- --distribution "$build/distribution.xml" --resources "$build/resources" --package-path "$build"
if [ -n "${SIGN_ID:-}" ]; then
    set -- "$@" --sign "$SIGN_ID"
fi
productbuild "$@" "$out"

if [ -n "${SIGN_ID:-}" ] && [ -n "${NOTARY_PROFILE:-}" ]; then
    xcrun notarytool submit "$out" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$out"
fi

rm -rf "$build/root" "$build/empty" "$build/resources" "$build/distribution.xml" \
       "$build/tailmon-core.pkg" "$build/tailscale-cli.pkg"
echo "$out"
