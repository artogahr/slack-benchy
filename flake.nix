{
  description = "slack-benchy: Benchy in Slack, talking to a Prusa printer via PrusaLink. Socket Mode, self-hosted, single-tenant.";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    let
      # NixOS module wraps the bare module with a default package built by
      # this flake, so users only need `imports = [ ... ];` plus their config.
      nixosModules.default = { pkgs, lib, ... }: {
        imports = [ ./nix/module.nix ];
        services.slack-benchy.package =
          lib.mkDefault self.packages.${pkgs.system}.default;
      };
      nixosModules.slack-benchy = nixosModules.default;
    in
    {
      inherit nixosModules;
    } // flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };

        slack-benchy = pkgs.python3Packages.buildPythonApplication {
          pname = "slack-benchy";
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
            mainProgram = "slack-benchy";
          };
        };
      in
      {
        packages.default = slack-benchy;
        packages.slack-benchy = slack-benchy;

        apps.default = flake-utils.lib.mkApp {
          drv = slack-benchy;
        };

        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            uv
            python312
            ruff
          ];
          shellHook = ''
            echo "slack-benchy dev shell"
            echo "  Setup: uv venv && uv pip install -e '.[dev]'"
            echo "  Test:  .venv/bin/pytest -q"
            echo "  Run:   set -a; source .env; set +a; .venv/bin/python -m slack_benchy"
            echo ""
            echo "Never put real secrets in shell.nix or flake.nix — use sops-nix or"
            echo "agenix when deploying via the NixOS module."
          '';
        };
      });
}
