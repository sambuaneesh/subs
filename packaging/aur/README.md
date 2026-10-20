# AUR package

`subs` builds the tagged release using Arch's system Python and dependencies.
`python-pysubs2` is an AUR dependency; the remaining dependencies are in the official repositories.

```sh
makepkg -si
```

For a new release, update `pkgver`, reset `pkgrel` to 1, run `updpkgsums`, then:

```sh
makepkg --printsrcinfo > .SRCINFO
makepkg --cleanbuild
```

Publish only `PKGBUILD` and `.SRCINFO` to `ssh://aur@aur.archlinux.org/subs.git`.
The upstream source and the release tag belong in the GitHub repository.
