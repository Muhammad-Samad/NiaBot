"""
Defines schemas for all tools the Gemini LLM is permitted to invoke.
No execution logic is defined here.
"""

TOOLS_DEFINITIONS = [
    {
        "name": "track_order",
        "description": "Fetches the current status and details of an order using its order ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "The unique alphanumeric identifier of the order"
                }
            },
            "required": ["order_id"]
        }
    },
    {
        "name": "create_complaint",
        "description": "Registers a new complaint for an order.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "The related order ID"
                },
                "issue_description": {
                    "type": "string",
                    "description": "Description of the issue or complaint"
                }
            },
            "required": ["issue_description"]
        }
    },
    {
        "name": "general_query",
        "description": "Handles general FAQ questions and brand inquiries.",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The user's question"
                }
            },
            "required": ["question"]
        }
    }
]
