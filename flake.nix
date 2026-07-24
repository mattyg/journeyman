{
  description = "FreeCAD LLM Copilot — development environment";

  # Pin to the stable release channel. Its FreeCAD/VTK are in the binary cache, so
  # the dev shell substitutes prebuilt binaries instead of compiling from source
  # (source builds fail: VTK does not compile under gcc 15 on nixos-unstable).
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        # Pure-Python test tier: our agent/llm_client/settings/deps modules import
        # neither FreeCAD nor Qt (and the LLM client is stdlib-only — urllib+json),
        # so plain CPython + pytest runs them with no third-party packages.
        pythonEnv = pkgs.python3.withPackages (ps: with ps; [
          pytest
        ]);
      in
      {
        devShells.default = pkgs.mkShell {
          packages = [
            pkgs.freecad      # provides `freecadcmd` + bundled Python for the integration tier
            pythonEnv         # provides `python3 -m pytest` for the pure-Python tier
          ];

          shellHook = ''
            echo "FreeCAD LLM Copilot dev shell"
            echo "  pure-Python tests : python3 -m pytest tests/test_*.py -v"
            echo "  integration tests : freecadcmd tests/integration/run_headless.py"
            export PYTHONPATH="$PWD:$PYTHONPATH"
          '';
        };
      });
}
