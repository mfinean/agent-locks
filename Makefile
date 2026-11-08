# make test          pytest, and shellcheck on the launchers
# make lint          ruff, with ruff.toml's rules
# make install       install lockview and agent-gate for this user (~/.local): rerun it to update them
# make pin           add lockview to the dock
# make uninstall     remove the installed copy, launcher and icon, and unpin it

PREFIX ?= $(HOME)/.local
APP_ID := io.github.mfinean.lockview
LIB := $(PREFIX)/share/agent-locks
APPS := $(PREFIX)/share/applications
ICONS := $(PREFIX)/share/icons/hicolor/scalable/apps
FAVORITES := from gi.repository import Gio; s = Gio.Settings.new("org.gnome.shell"); f = s.get_strv("favorite-apps"); a = "$(APP_ID).desktop"

.PHONY: test lint install uninstall pin unpin

test:
	python3 -m pytest -q
	shellcheck bin/lockview bin/agent-gate

lint:
	ruff check .

# A copy, not a link to this checkout, so a half-made edit here can't break the dock's launcher.
# An open window keeps running the copy it started with until it is closed.
install:
	rm -rf $(LIB).new
	mkdir -p $(LIB).new $(PREFIX)/bin $(APPS) $(ICONS)
	cp -r bin lockview share $(LIB).new/
	find $(LIB).new -name __pycache__ -prune -exec rm -rf {} +
	git describe --always --dirty > $(LIB).new/VERSION
	rm -rf $(LIB).old
	if [ -e $(LIB) ]; then mv $(LIB) $(LIB).old; fi
	mv $(LIB).new $(LIB)
	rm -rf $(LIB).old
	ln -sfn $(LIB)/bin/lockview $(PREFIX)/bin/lockview
	ln -sfn $(LIB)/bin/agent-gate $(PREFIX)/bin/agent-gate
	install -m644 share/$(APP_ID).svg $(ICONS)/
	sed 's|@BIN@|$(PREFIX)/bin/lockview|' share/$(APP_ID).desktop.in > $(APPS)/$(APP_ID).desktop
	@echo "installed $$(cat $(LIB)/VERSION): lockview ('make pin' puts it in the dock) and $(PREFIX)/bin/agent-gate"

# The dock pins the launcher by name, and only the one in ~/.local/share/applications is the dock's:
# uninstalling a copy from another PREFIX leaves the pin alone.
uninstall: $(if $(filter $(HOME)/.local,$(PREFIX)),unpin)
	rm -rf $(LIB)
	if [ -L $(PREFIX)/bin/lockview ]; then rm $(PREFIX)/bin/lockview; fi
	if [ -L $(PREFIX)/bin/agent-gate ]; then rm $(PREFIX)/bin/agent-gate; fi
	rm -f $(APPS)/$(APP_ID).desktop $(ICONS)/$(APP_ID).svg

# Settings writes are asynchronous: sync() before exiting, or the change can be lost.
pin:
	python3 -c '$(FAVORITES); a in f or s.set_strv("favorite-apps", f + [a]); Gio.Settings.sync()'

unpin:
	python3 -c '$(FAVORITES); a in f and s.set_strv("favorite-apps", [x for x in f if x != a]); Gio.Settings.sync()'
