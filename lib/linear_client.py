"""
Linear GraphQL API client — replaces the @linear/sdk npm package.
"""

from __future__ import annotations
from typing import Any

import requests

API_URL = "https://api.linear.app/graphql"


class LinearClient:
    def __init__(self, api_key: str) -> None:
        self._headers = {
            "Authorization": api_key,
            "Content-Type": "application/json",
        }

    # ── low-level ────────────────────────────────────────────────────

    def _gql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = requests.post(API_URL, json={"query": query, "variables": variables or {}}, headers=self._headers)
        resp.raise_for_status()
        body = resp.json()
        if "errors" in body:
            raise RuntimeError(f"Linear API errors: {body['errors']}")
        return body["data"]

    # ── viewer ───────────────────────────────────────────────────────

    def get_viewer(self) -> dict[str, Any]:
        data = self._gql("{ viewer { id name email } }")
        return data["viewer"]

    # ── teams ────────────────────────────────────────────────────────

    def get_teams(self) -> list[dict[str, Any]]:
        data = self._gql("{ teams { nodes { id name key } } }")
        return data["teams"]["nodes"]

    # ── issues ───────────────────────────────────────────────────────

    def get_issues(self, team_id: str, assignee_id: str, first: int = 20) -> list[dict[str, Any]]:
        query = """
        query($teamId: ID!, $assigneeId: ID!, $first: Int!) {
          issues(
            filter: {
              team: { id: { eq: $teamId } }
              state: { type: { in: ["unstarted", "started"] } }
              assignee: { id: { eq: $assigneeId } }
            }
            first: $first
          ) {
            nodes {
              id
              identifier
              title
              description
              url
              priority
              createdAt
              updatedAt
            }
          }
        }
        """
        data = self._gql(query, {"teamId": team_id, "assigneeId": assignee_id, "first": first})
        return data["issues"]["nodes"]

    # ── labels ───────────────────────────────────────────────────────

    def get_issue_labels(self, issue_id: str) -> list[str]:
        query = """
        query($id: ID!) {
          issue(id: $id) { labels { nodes { name } } }
        }
        """
        data = self._gql(query, {"id": issue_id})
        return [l["name"] for l in data["issue"]["labels"]["nodes"]]

    # ── project ──────────────────────────────────────────────────────

    def get_issue_project_name(self, issue_id: str) -> str | None:
        query = """
        query($id: ID!) {
          issue(id: $id) { project { name } }
        }
        """
        data = self._gql(query, {"id": issue_id})
        project = data["issue"].get("project")
        return project["name"] if project else None

    # ── state / transition ───────────────────────────────────────────

    def get_team_states(self, team_id: str) -> list[dict[str, Any]]:
        query = """
        query($id: ID!) {
          team(id: $id) { states { nodes { id name type } } }
        }
        """
        data = self._gql(query, {"id": team_id})
        return data["team"]["states"]["nodes"]

    def get_issue_team_id(self, issue_id: str) -> str | None:
        query = """
        query($id: ID!) {
          issue(id: $id) { team { id } }
        }
        """
        data = self._gql(query, {"id": issue_id})
        team = data["issue"].get("team")
        return team["id"] if team else None

    def update_issue(self, issue_id: str, state_id: str) -> None:
        mutation = """
        mutation($id: ID!, $stateId: String!) {
          issueUpdate(id: $id, input: { stateId: $stateId }) { success }
        }
        """
        self._gql(mutation, {"id": issue_id, "stateId": state_id})

    # ── comments ─────────────────────────────────────────────────────

    def get_issue_comments(self, issue_id: str, first: int = 50) -> list[dict[str, Any]]:
        query = """
        query($id: ID!, $first: Int!) {
          issue(id: $id) {
            comments(first: $first) {
              nodes {
                body
                createdAt
                user { name }
              }
            }
          }
        }
        """
        data = self._gql(query, {"id": issue_id, "first": first})
        return data["issue"]["comments"]["nodes"]

    def create_comment(self, issue_id: str, body: str) -> None:
        mutation = """
        mutation($issueId: String!, $body: String!) {
          commentCreate(input: { issueId: $issueId, body: $body }) { success }
        }
        """
        self._gql(mutation, {"issueId": issue_id, "body": body})

    # ── children / sub-issues ────────────────────────────────────────

    def get_issue_children(self, issue_id: str, first: int = 20) -> list[dict[str, Any]]:
        query = """
        query($id: ID!, $first: Int!) {
          issue(id: $id) {
            children(first: $first) {
              nodes {
                identifier
                title
                description
                state { name }
              }
            }
          }
        }
        """
        data = self._gql(query, {"id": issue_id, "first": first})
        return data["issue"]["children"]["nodes"]

    # ── parent ───────────────────────────────────────────────────────

    def get_issue_parent(self, issue_id: str) -> dict[str, Any] | None:
        query = """
        query($id: ID!) {
          issue(id: $id) {
            parent {
              identifier
              title
              description
            }
          }
        }
        """
        data = self._gql(query, {"id": issue_id})
        return data["issue"].get("parent")

    # ── relations ────────────────────────────────────────────────────

    def get_issue_relations(self, issue_id: str, first: int = 20) -> list[dict[str, Any]]:
        query = """
        query($id: ID!, $first: Int!) {
          issue(id: $id) {
            relations(first: $first) {
              nodes {
                type
                relatedIssue {
                  identifier
                  title
                  description
                }
              }
            }
          }
        }
        """
        data = self._gql(query, {"id": issue_id, "first": first})
        return data["issue"]["relations"]["nodes"]

    # ── attachments ──────────────────────────────────────────────────

    def get_issue_attachments(self, issue_id: str, first: int = 10) -> list[dict[str, Any]]:
        query = """
        query($id: ID!, $first: Int!) {
          issue(id: $id) {
            attachments(first: $first) {
              nodes {
                title
                url
              }
            }
          }
        }
        """
        data = self._gql(query, {"id": issue_id, "first": first})
        return data["issue"]["attachments"]["nodes"]

    # ── issue state ──────────────────────────────────────────────────

    def get_issue_state(self, issue_id: str) -> dict[str, Any] | None:
        query = """
        query($id: ID!) {
          issue(id: $id) { state { name type } }
        }
        """
        data = self._gql(query, {"id": issue_id})
        return data["issue"].get("state")
