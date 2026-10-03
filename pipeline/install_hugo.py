#!/usr/bin/env python3
"""Install the checksum-pinned local test Hugo, not a Theme or deployment tool."""
import sys
sys.dont_write_bytecode = True

from publisher.tooling import install_hugo

if __name__ == "__main__":
    print(install_hugo())
