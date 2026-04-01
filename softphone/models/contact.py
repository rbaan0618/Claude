"""Contact entry model."""

from dataclasses import dataclass


@dataclass
class Contact:
    name: str
    number: str
    favorite: bool = False
    protocol: str = "SIP"

    def to_dict(self):
        return {
            "name": self.name,
            "number": self.number,
            "favorite": self.favorite,
            "protocol": self.protocol,
        }

    @staticmethod
    def from_dict(d):
        return Contact(
            name=d.get("name", ""),
            number=d.get("number", ""),
            favorite=d.get("favorite", False),
            protocol=d.get("protocol", "SIP"),
        )
