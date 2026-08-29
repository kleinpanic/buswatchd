PROJECT := buswatchd

PREFIX ?= $(HOME)/.local
BIN_DIR := $(PREFIX)/bin

SYSTEMD_USER_DIR := $(HOME)/.config/systemd/user
CONFIG_DIR := $(HOME)/.config/$(PROJECT)

BIN_NAME := $(PROJECT)
UNIT_NAME := $(PROJECT).service

SRC_BIN := src/$(PROJECT).py
UNIT_SRC := systemd/$(UNIT_NAME)
CFG_SRC  := config/config.json

.PHONY: all install uninstall enable disable status logs

all:
	@echo "Targets: install uninstall enable disable status logs"

install:
	@mkdir -p "$(BIN_DIR)"
	@install -m 0755 "$(SRC_BIN)" "$(BIN_DIR)/$(BIN_NAME)"
	@mkdir -p "$(SYSTEMD_USER_DIR)"
	@install -m 0644 "$(UNIT_SRC)" "$(SYSTEMD_USER_DIR)/$(UNIT_NAME)"
	@mkdir -p "$(CONFIG_DIR)"
	@if [ ! -f "$(CONFIG_DIR)/config.json" ]; then \
		install -m 0644 "$(CFG_SRC)" "$(CONFIG_DIR)/config.json"; \
		echo "Installed default config to $(CONFIG_DIR)/config.json"; \
	else \
		echo "Config exists, not overwriting: $(CONFIG_DIR)/config.json"; \
	fi
	@systemctl --user daemon-reload
	@systemctl --user enable --now "$(UNIT_NAME)"
	@echo "Installed and started $(UNIT_NAME)"

uninstall: disable
	@rm -f "$(BIN_DIR)/$(BIN_NAME)"
	@rm -f "$(SYSTEMD_USER_DIR)/$(UNIT_NAME)"
	@systemctl --user daemon-reload
	@echo "Removed binary and unit. Config/state left in $(CONFIG_DIR) (delete manually if you want)."

enable:
	@systemctl --user enable --now "$(UNIT_NAME)"

disable:
	-@systemctl --user disable --now "$(UNIT_NAME)" >/dev/null 2>&1 || true

status:
	@systemctl --user status "$(UNIT_NAME)" --no-pager || true

logs:
	@journalctl --user -u "$(UNIT_NAME)" -n 200 --no-pager
