# Maintainer: Daniel Mouritsen
#
# Build and install from this directory:
#   makepkg -si
#
# Sources are the files in this repo, so checksums are skipped; nothing is
# downloaded.

pkgname=frigate-watchdog
pkgver=0.1.0
pkgrel=1
pkgdesc="Detects Frigate cameras that have stopped recording and recovers them (camera bounce, go2rtc restart, Frigate restart)"
arch=('any')
license=('MIT')
depends=('python' 'systemd')
optdepends=('python-paho-mqtt: per-camera bounce via MQTT before restarting go2rtc'
            'docker: fallback go2rtc restart via docker exec')
backup=('etc/frigate-watchdog.env')
install=frigate-watchdog.install
source=('frigate_watchdog.py'
        'frigate-watchdog.service'
        'frigate-watchdog.env')
sha256sums=('SKIP' 'SKIP' 'SKIP')

package() {
  install -Dm755 "$srcdir/frigate_watchdog.py"      "$pkgdir/usr/bin/frigate-watchdog"
  install -Dm644 "$srcdir/frigate-watchdog.service" "$pkgdir/usr/lib/systemd/system/frigate-watchdog.service"
  # 600: may hold the MQTT password. systemd reads it as root before dropping privileges.
  install -Dm600 "$srcdir/frigate-watchdog.env"     "$pkgdir/etc/frigate-watchdog.env"
}
