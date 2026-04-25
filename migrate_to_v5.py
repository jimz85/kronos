#!/usr/bin/env python3
"""
================================================================================
migrate_to_v5.py - Migration Script for Kronos V4 to V5
================================================================================

This script migrates configuration and data from Kronos V4 to V5.
It handles:
  - Configuration file updates
  - Data schema changes
  - Strategy parameter migrations
  - Risk management parameter updates

Version: 5.0.0
================================================================================
"""

import os
import sys
import json
import shutil
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional, List


# Add the project root to the path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

# Import new constants
try:
    from constants import (
        SYSTEM_NAME,
        SYSTEM_VERSION,
        DEFAULT_RISK_CONFIG,
        V4_TO_V5_MIGRATION_VERSION,
        MIGRATION_REQUIRED_FIELDS,
    )
except ImportError as e:
    print(f"Warning: Could not import constants: {e}")
    SYSTEM_NAME = "Kronos V5"
    SYSTEM_VERSION = "5.0.0"


class MigrationError(Exception):
    """Custom exception for migration errors."""
    pass


class V4toV5Migrator:
    """
    Migrator class for V4 to V5 migration.
    
    Handles:
      - Configuration updates
      - Data file migrations
      - Schema transformations
    """
    
    def __init__(self, project_root: Path):
        """
        Initialize the migrator.
        
        Args:
            project_root: Path to the project root directory
        """
        self.project_root = project_root
        self.backup_dir = project_root / f"backup_v4_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.migration_log: List[str] = []
        
    def log(self, message: str) -> None:
        """Log a migration message."""
        timestamp = datetime.now().isoformat()
        log_entry = f"[{timestamp}] {message}"
        self.migration_log.append(log_entry)
        print(log_entry)
    
    def create_backup(self) -> None:
        """Create a backup of existing files before migration."""
        self.log(f"Creating backup at: {self.backup_dir}")
        self.backup_dir.mkdir(exist_ok=True)
        
        # Backup key directories
        dirs_to_backup = ["core", "strategies", "models", "risk", "execution", "data"]
        for dir_name in dirs_to_backup:
            src = self.project_root / dir_name
            if src.exists():
                dst = self.backup_dir / dir_name
                self.log(f"  Backing up: {dir_name}/")
                shutil.copytree(src, dst, dirs_exist_ok=True)
    
    def migrate_config(self, config_path: Path) -> Dict[str, Any]:
        """
        Migrate a V4 configuration file to V5 format.
        
        Args:
            config_path: Path to the V4 configuration file
            
        Returns:
            Migrated configuration dictionary
        """
        self.log(f"Migrating configuration: {config_path}")
        
        if not config_path.exists():
            self.log(f"  Warning: Config file not found: {config_path}")
            return {}
        
        with open(config_path, 'r') as f:
            config = json.load(f)
        
        # V4 -> V5 migrations
        migrated = config.copy()
        
        # Add new V5 fields
        migrated["version"] = SYSTEM_VERSION
        migrated["migrated_at"] = datetime.now().isoformat()
        migrated["system"] = SYSTEM_NAME
        
        # Migrate risk parameters
        if "risk" in migrated:
            risk = migrated["risk"]
            if "max_position" in risk:
                risk["max_position_size"] = risk.pop("max_position")
        
        # Migrate strategy parameters
        if "strategy" in migrated:
            strategy = migrated["strategy"]
            if "type" in strategy:
                strategy["strategy_type"] = strategy.pop("type")
        
        self.log(f"  Configuration migrated successfully")
        return migrated
    
    def migrate_data_schema(self, data_path: Path) -> bool:
        """
        Migrate a V4 data file to V5 schema.
        
        Args:
            data_path: Path to the V4 data file
            
        Returns:
            True if migration successful
        """
        self.log(f"Migrating data schema: {data_path}")
        
        if not data_path.exists():
            self.log(f"  Warning: Data file not found: {data_path}")
            return False
        
        # Read V4 data
        with open(data_path, 'r') as f:
            data = json.load(f)
        
        # Apply V5 schema transformations
        # (This is a placeholder - actual transformations would depend on V4 schema)
        migrated_data = data.copy()
        migrated_data["schema_version"] = SYSTEM_VERSION
        migrated_data["migrated_at"] = datetime.now().isoformat()
        
        # Write migrated data
        backup_path = data_path.with_suffix('.json.v4_backup')
        shutil.copy(data_path, backup_path)
        
        with open(data_path, 'w') as f:
            json.dump(migrated_data, f, indent=2)
        
        self.log(f"  Data schema migrated (backup: {backup_path})")
        return True
    
    def run(self) -> bool:
        """
        Run the complete migration process.
        
        Returns:
            True if migration successful
        """
        self.log("=" * 60)
        self.log(f"Starting V4 to V5 Migration: {SYSTEM_VERSION}")
        self.log("=" * 60)
        
        try:
            # Step 1: Create backup
            self.create_backup()
            
            # Step 2: Migrate configuration files
            config_files = [
                self.project_root / "config.json",
                self.project_root / "config" / "default.json",
                self.project_root / ".env",
            ]
            
            for config_file in config_files:
                if config_file.exists() and config_file.suffix == '.json':
                    self.migrate_config(config_file)
            
            # Step 3: Migrate data schemas
            data_dir = self.project_root / "data"
            if data_dir.exists():
                for data_file in data_dir.glob("*.json"):
                    self.migrate_data_schema(data_file)
            
            # Step 4: Create __init__.py files in new directories
            init_dirs = ["core", "strategies", "models", "risk", "execution"]
            for dir_name in init_dirs:
                init_file = self.project_root / dir_name / "__init__.py"
                if not init_file.exists():
                    init_file.write_text(f'"""Kronos V5 {dir_name.capitalize()} Module"""\n')
                    self.log(f"  Created: {init_file}")
            
            self.log("=" * 60)
            self.log("Migration completed successfully!")
            self.log("=" * 60)
            
            # Save migration log
            log_file = self.project_root / f"migration_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            with open(log_file, 'w') as f:
                f.write('\n'.join(self.migration_log))
            self.log(f"Migration log saved to: {log_file}")
            
            return True
            
        except Exception as e:
            self.log(f"ERROR: Migration failed: {e}")
            raise MigrationError(f"Migration failed: {e}") from e


def main():
    """Main entry point for the migration script."""
    project_root = Path(__file__).parent
    
    print(f"Kronos V4 to V5 Migration Tool")
    print(f"Project Root: {project_root}")
    print()
    
    # Confirm before proceeding
    response = input("This will backup your existing files and migrate to V5. Continue? (y/n): ")
    if response.lower() != 'y':
        print("Migration cancelled.")
        return 1
    
    try:
        migrator = V4toV5Migrator(project_root)
        success = migrator.run()
        return 0 if success else 1
    except MigrationError as e:
        print(f"Migration Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
