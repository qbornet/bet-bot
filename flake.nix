{
  description = "Discord Betting Bot Development Environment";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs {
            inherit system;
            config = {
                allowUnfree = true;
            };
        };

        #nixpkgs.legacyPackages.${system};
        pythonPackages = pkgs.python312Packages;
      in
      {
        devShells.default = pkgs.mkShell {
          name = "bet-bot-env";
          
          buildInputs = with pkgs; [
            python312
            python312Packages.pip
            python312Packages.virtualenv
            google-chrome
            chromedriver
            git
          ];

          shellHook = ''
            echo "🎮 Discord Betting Bot Development Environment"
            echo "=============================================="
            echo ""
            echo "Available commands:"
            echo "  python bot/main.py    - Start the bot"
            echo "  pip install -r requirements.txt  - Install dependencies"
            echo ""
            echo "Make sure to create a .env file with:"
            echo "  DISCORD_TOKEN=your_token_here"
            echo "  ADMIN_USER_IDS=your_discord_id"
            echo ""
            
            # Create virtual environment if it doesn't exist
            if [ ! -d .venv ]; then
              echo "Creating virtual environment..."
              virtualenv .venv
            fi
            
            # Activate virtual environment
            source .venv/bin/activate
            
            # Install requirements if not already installed
            if [ -f requirements.txt ]; then
              pip install -q -r requirements.txt 2>/dev/null || true
            fi
            
            # Create data directory if it doesn't exist
            mkdir -p data
          '';
        };
      });
}
