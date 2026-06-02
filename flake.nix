{
  description = "Prusa printer Slack bot — Socket Mode + PrusaLink, self-hosted, single-tenant.";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    let
      # NixOS module is system-agnostic; expose it at the top level.
      nixosModules.default = import ./nix/module.nix;
      nixosModules.prusa-slack-bot = nixosModules.default;
    in
    {
      inherit nixosModules;
    } // flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };

        prusa-slack-bot = pkgs.python3Packages.buildPythonApplication {
          pname = "prusa-slack-bot";
          version = "0.1.0";
          src = ./.;
          pyproject = true;
          build-system = [ pkgs.python3Packages.hatchling ];
          propagatedBuildInputs = with pkgs.python3Packages; [
            slack-bolt
            slack-sdk
            aiohttp
            httpx
            pyyaml
          ];
          # No tests in the package build path — tests live under ./tests and
          # exercise sqlite/asyncio; run them in the dev shell instead.
          doCheck = false;
          meta = with pkgs.lib; {
            description = "Slack bot for Prusa 3D printers via PrusaLink + Socket Mode";
            license = licenses.mit;
            mainProgram = "prusa-slack-bot";
          };
        };
      in
      {
        packages.default = prusa-slack-bot;
        packages.prusa-slack-bot = prusa-slack-bot;

        apps.default = flake-utils.lib.mkApp {
          drv = prusa-slack-bot;
        };

        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            uv
            python312
            ruff
          ];
          shellHook = ''
            echo "prusa-slack-bot dev shell"
            echo "  Setup: uv venv && uv pip install -e '.[dev]'"
            echo "  Test:  .venv/bin/pytest -q"
            echo "  Run:   set -a; source .env; set +a; .venv/bin/python -m prusa_slack_bot"
            echo ""
            echo "Never put real secrets in shell.nix or flake.nix — use sops-nix or"
            echo "agenix when deploying via the NixOS module."
          '';
        };
      });
}
