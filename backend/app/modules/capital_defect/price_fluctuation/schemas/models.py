from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field


class TextBlock(BaseModel):
    page: int
    text: str


class TableBlock(BaseModel):
    page: int
    table_index: int
    rows: list[list[Optional[str]]]


class SectionChunk(BaseModel):
    title: str
    content: str
    page_start: int
    page_end: int


class TransferEvent(BaseModel):
    time: Optional[date] = None
    transferor: Optional[str] = None
    transferee: Optional[str] = None
    shares: Optional[Decimal] = None
    amount_cny: Optional[Decimal] = None
    unit_price_cny_per_share: Optional[Decimal] = None
    currency: str = "CNY"
    source_pages: list[int] = Field(default_factory=list)


class CapitalIncreaseEvent(BaseModel):
    time: Optional[date] = None
    investor: Optional[str] = None
    shares: Optional[Decimal] = None
    amount_cny: Optional[Decimal] = None
    unit_price_cny_per_share: Optional[Decimal] = None
    currency: str = "CNY"
    source_pages: list[int] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    transfer_events: list[TransferEvent] = Field(default_factory=list)
    increase_events: list[CapitalIncreaseEvent] = Field(default_factory=list)


class TimelineRow(BaseModel):
    time: date
    event: Literal["股权转让", "增资"]
    transferor: str
    transferee: str
    unit_price_cny_per_share: Decimal
    page: int
    source_event_id: str


class FluctuationAlert(BaseModel):
    message: str
    page: int
    previous_price: Decimal
    current_price: Decimal
    change_ratio: Decimal
    previous_event_id: str
    current_event_id: str
    previous_event_page: Optional[int] = None
    current_event_page: Optional[int] = None
    previous_event_text: Optional[str] = None
    current_event_text: Optional[str] = None


class PriceFluctuationOutput(BaseModel):
    timeline: list[TimelineRow]
    alerts: list[FluctuationAlert]
