GENERAL_POLICY_KB = {
    "delivery": {
        "title": "Delivery & Shipping Policy",
        # Alternate names/phrasings customers use that should also map to this
        # topic (e.g. "roadside pickup" or "express delivery" on their own,
        # without the word "delivery"/"shipping" attached).
        "keywords": [
            "delivery", "shipping", "roadside pickup", "road-side pickup",
            "road side pickup", "pickup", "express delivery", "express shipping",
            "same day delivery", "same-day delivery"
        ],
        # Specific shipping-method phrases -> the exact bullet-point substring
        # to answer with, instead of dumping the entire delivery policy.
        # e.g. asking about "roadside pickup" should only return the
        # Road-Side Pickup line, not Standard/Express/etc. as well.
        "specific_topics": {
            "roadside pickup": "road-side pickup",
            "road-side pickup": "road-side pickup",
            "road side pickup": "road-side pickup",
            "pickup": "road-side pickup",
            "express delivery": "express shipping",
            "express shipping": "express shipping",
            "same day delivery": "express shipping",
            "same-day delivery": "express shipping",
        },
        "sections": [
            {
                "subtitle": "Karachi (including Bahria Town & adjoining areas)",
                "points": [
                    "Standard Shipping: 48-72 Hours (Rs.99 per order)",
                    "Express Shipping: Same-day for orders placed between 9:00 AM and 4:00 PM (Rs.299 per order)",
                    "Road-Side Pickup: Within 2 working days for orders placed between 10:00 AM and 8:00 PM (Free, Pre-paid orders only)"
                ]
            },
            {
                "subtitle": "Lahore & Islamabad",
                "points": [
                    "Delivery Time: 3-5 working days",
                    "Orders under 5 KG: Rs.199",
                    "Orders above 5 KG: Rs.199 + Rs.75 per additional KG"
                ]
            },
            {
                "subtitle": "Other Cities",
                "points": [
                    "Delivery Time: 3-5 working days",
                    "Orders under 5 KG: Rs.199",
                    "Orders above 5 KG: Rs.199 + Rs.75 per additional KG"
                ]
            }
        ]
    },
    "payment": {
        "title": "Payment Methods",
        "sections": [
            {
                "points": [
                    "Cash on Delivery",
                    "Online Visa",
                    "Online MasterCard",
                    "Credit/Debit Card on Delivery",
                    "UnionPay Debit/Credit Cards"
                ]
            }
        ]
    },
    "otp": {
        "title": "OTP Verification",
        "sections": [
            {
                "points": [
                    "If you cannot receive an OTP after multiple attempts, please call us at (021) 111-624-333.",
                    "Phone verification must be completed using the same registered mobile number."
                ]
            }
        ]
    },
    "loyalty": {
        "title": "Loyalty Program",
        "sections": [
            {
                "points": [
                    "Yes, Naheed.pk offers the exclusive Naheed Loyalty Program."
                ]
            }
        ]
    },
    "returns": {
        "title": "Return Policy",
        "sections": [
            {
                "points": [
                    "Returns must be initiated within 7 days of delivery. Requests after 7 days cannot be accepted.",
                    "Customers do NOT pay shipping charges when returning a product."
                ]
            }
        ]
    },
    "warranty": {
        "title": "Warranty Policy",
        "sections": [
            {
                "points": [
                    "Warranty varies by product and vendor.",
                    "Naheed.pk does not process warranty claims directly. Warranty claims are handled by the respective vendor's service center.",
                    "Not every product includes a warranty. If a warranty exists, it is mentioned in the product specifications."
                ]
            }
        ]
    },
    "company": {
        "title": "Company Information",
        "sections": [
            {
                "points": [
                    "Naheed.pk is the official e-commerce platform of Naheed Supermarket.",
                    "We currently deliver to more than 800 cities across Pakistan."
                ]
            }
        ]
    }
}
