{
  description = "FreeCAD LLM Copilot — development environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        # Pure-Python test tier: our agent/llm_client/settings/deps modules import
        # neither FreeCAD nor Qt, so plain CPython + pytest runs them. litellm is
        # included so llm_client can be exercised; it is monkeypatched in tests but
        # importing the module must not fail.
        pythonEnv = pkgs.python3.withPackages (ps: with ps; [
          pytest
          litellm
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
