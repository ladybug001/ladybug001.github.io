#!/usr/bin/env python3
"""Install the shared checksum-pinned Hugo Extended, without global changes."""
import sys
sys.dont_write_bytecode = True

from publisher.tooling import install_hugo

if __name__ == "__main__":
    print(install_hugo())
