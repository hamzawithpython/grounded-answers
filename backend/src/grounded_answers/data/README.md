# Data files

## common_passwords.txt

The 10,000 most common passwords, one per line, lowercase. Used by the password policy
(`grounded_answers/passwords.py`, ADR 0004).

- **Source:** `Passwords/Common-Credentials/10k-most-common.txt` in
  [SecLists](https://github.com/danielmiessler/SecLists)
- **Pinned commit:** `913b327317496d062bcc7cace524aaad8a693be2`
- **License:** MIT (SecLists LICENSE, Copyright (c) 2018 Daniel Miessler). The file is used
  unmodified.

To update it, fetch the file from a newer pinned commit, replace the file, and record the new
commit here. Do not edit the list by hand.
