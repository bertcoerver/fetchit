.PHONY: release clean build upload addon-release

release: clean build upload

# Builds and publishes the Home Assistant add-on on GitHub Actions; Home
# Assistant then offers it as an update. See .github/workflows/addon.yml.
addon-release:
	@test -n "$(VERSION)" || { echo "usage: make addon-release VERSION=x.y.z"; exit 1; }
	gh workflow run addon.yml --ref main -f version=$(VERSION)
	@echo "Started. Follow it with: gh run watch"

clean:
	rm -rf dist/ build/ src/*.egg-info

build:
	python -m build

upload:
	twine upload dist/*
