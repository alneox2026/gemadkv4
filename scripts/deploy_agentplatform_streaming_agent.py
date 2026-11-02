#!/usr/bin/env python3
"""
Deploy any local ADK Agent to Google Agent Platform (Vertex AI Reasoning Engine).

Usage:
  python deploy_agentplatform_streaming_agent.py --agent-name maksat --model gemini-3.8-flash
"""

from __future__ import annotations

import argparse
import os
import sys

import vertexai
from vertexai.preview import reasoning_engines


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy ADK Agent to Agent Platform")
    parser.add_argument("--project-id", default=os.getenv("PROJECT_ID", "ceo-dev123"))
    parser.add_argument("--region", default=os.getenv("REGION", "us-central1"))
    parser.add_argument("--agent-name", default="my-agentplatform-agent")
    parser.add_argument("--model", default="gemini-3.8-flash")
    parser.add_argument("--staging-bucket", default=None)
    args = parser.parse_args()

    project_id = args.project_id
    region = args.region
    agent_name = args.agent_name
    model_name = args.model
    staging_bucket = args.staging_bucket or f"gs://{project_id}-reasoning-engine-staging"

    print("=" * 65)
    print(" Deploying ADK Agent to Vertex AI Agent Platform")
    print(f" Agent Name    : {agent_name}")
    print(f" Model         : {model_name}")
    print(f" Project ID    : {project_id}")
    print(f" Region        : {region}")
    print(f" Staging Bucket: {staging_bucket}")
    print("=" * 65)

    vertexai.init(
        project=project_id,
        location=region,
        staging_bucket=staging_bucket,
    )

    print("--> Creating Reasoning Engine on Agent Platform...")
    # NOTE: Replace 'MyAgentClass' with your actual root agent class or ADK App wrapper
    # Example:
    # app = reasoning_engines.ReasoningEngine.create(
    #     MyAgentClass(model=model_name),
    #     requirements=["google-adk>=0.1.0", "google-genai>=1.0.0"],
    #     display_name=agent_name,
    #     description=f"Agent Platform Streaming Agent ({model_name})",
    # )
    # resource_name = app.resource_name
    #
    # print("\n" + "=" * 65)
    # print(" Agent Deployed Successfully to Agent Platform!")
    # print(f" Resource Name: {resource_name}")
    # print("\nNow register this agent in config/agents.prod.yaml:")
    # print("-" * 50)
    # print(f"{agent_name}:")
    # print(f"  agent_id: {agent_name}")
    # print("  backend: agent_runtime")
    # print(f"  model: {model_name}")
    # print(f"  resource_name: {resource_name}")
    # print(f"  region: {region}")
    # print("  streaming_enabled: true")
    # print("  persistence_enabled: true")
    # print("  auth_policy: firebase")
    # print("=" * 65)


if __name__ == "__main__":
    main()
