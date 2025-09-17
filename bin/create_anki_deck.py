#!/usr/bin/env python3

import argparse
from dataclasses import dataclass
from enum import Enum
import itertools
import os
from pathlib import Path
import shutil

import re

import genanki
from genanki.util import guid_for

# TODO: Consider migrating to mistletoe-ebp:
# https://mistletoe-ebp.readthedocs.io/en/latest/index.html
# As part of the Executable Books Project.
# https://executablebooks.org

import mistletoe
from mistletoe import block_token, markdown_renderer, span_token
from mistletoe.token import Token
from mistletoe.block_tokenizer import FileWrapper
from mistletoe.html_renderer import HtmlRenderer


SQL_LITE_MAX_VALUE = 9223372036854775807


def validate_and_return_args():
    """Validate the commandline inputs and return args."""

    parser = argparse.ArgumentParser(
        prog="create_anki_deck.py",
        description="Creates an anki deck from a markdown file",
        epilog="""The command will create a new Anki deck with a file suffix added.
  The original markdown file will have a backup created and the original will be annotate
  with note numbers so that updates to notes will sync even when new notes are added.

  The markdown file uses some custom syntax to define notes.

  An example cloze note is:

  ```
  --- --- ---
  ***<!--c1::-->Non-excludable*** - people cannot be easily
  excluded from using it.
  ---
  Some interesting context on the back.
  --- ---
  ```

  An example front-back note is:

  ```
  --- --- ---
  Front
  ---
  Back
  --- ---
  ```
      """,
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument(
        "markdown_filename", help="the markdown file that will be processed"
    )
    parser.add_argument(
        "--print-anki",
        action="store_true",
        help="Will print output of the anki parser for debugging.",
    )
    parser.add_argument(
        "--print-walk",
        action="store_true",
        help="Will print the tokens from first to last in the document.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Test what would happen")

    parsed_args = parser.parse_args()

    if not os.path.exists(parsed_args.markdown_filename):
        parser.error(
            f"The markdown file {parsed_args.markdown_filename} does not exist!"
        )

    if not parsed_args.markdown_filename.endswith(".md"):
        parser.error(
            f"The markdown file {parsed_args.markdown_filename} does not end with '.md'!"
        )

    return parsed_args


class ClozeSpan(span_token.SpanToken):
    pattern = re.compile(r"\*\*\*<!--c([0-9]+)::(.*?)-->*(.+?)\*\*\*")
    parse_group = 3

    def __init__(self, match_obj):  # pylint: disable=W0231
        self.cloze_number = match_obj.group(1)
        self.cloze_hint = match_obj.group(2)

class EmptyNewLine(block_token.BlockToken):

    pattern = re.compile(r"^\n$")

    def __init__(self, content):  # pylint: disable=W0231
        self.content = ""
        self.children = [span_token.RawText("")]

    @classmethod
    def start(cls, line: str) -> bool:
        """Tests a line to see if it is the start of this block."""
        return line == "\n"

    @staticmethod
    def read(  # pyright: ignore[reportIncompatibleMethodOverride]
        lines: FileWrapper,
    ) -> str:
        """Reads lines until the block is complete. The return is used in the constructor"""
        line = next(lines)
        if line != "\n":
            raise ValueError("Internal issue parsing a new line")
        return "\n"  # Returning None has overhead.

class SrsNoteBlock(block_token.BlockToken):
    START_LINE = "--- --- ---"
    END_LINE = "--- ---"
    CARD_SEPARATOR_LINE = "---"
    pattern = re.compile(START_LINE + r"\n([\S\s]+?(|\n---([\S\s]+?)))\n" + END_LINE)
    pattern_note_number = re.compile(r"<!--note::([0-9]+)-->")

    def __init__(self, match):  # pylint: disable=W0231
        """Constructs an SrsNoteBlock using the output of the `read` method."""

        front_lines, back_lines, note_number = match

        self.note_number = note_number

        self.front_content = '\n'.join([line.strip() for line in (front_lines)])
        self.back_content = '\n'.join([line.strip() for line in (back_lines)])
        self.children_front: list[Token] = list(block_token.tokenize(front_lines))
        self.children_back: list[Token] = list(block_token.tokenize(back_lines))
        # TODO: Replace this with adding children with token types.
        # Our code doesn't use children, but fudge it for other code.
        self.children = self.children_front + self.children_back

    @classmethod
    def start(cls, line: str) -> bool:
        """Tests a line to see if it is the start of this block."""
        return cls.START_LINE in line

    @staticmethod
    def read(  # pyright: ignore[reportIncompatibleMethodOverride]
        lines: FileWrapper,
    ) -> tuple[
        # The return type of this function is expected to be overloaded.
        # The output is used as the input to the constructor of the class.
        list[str], list[str], int | None
    ]:
        """Reads lines until the block is complete. The return is used in the constructor"""
        front_lines = []
        back_lines = []
        note_number = None
        is_front = True
        next(lines) # Skip the line with three hr.
        for line in lines:
            match = SrsNoteBlock.pattern_note_number.match(line)
            if (match and match.group(1)):
                note_number = int(match.group(1))
                continue
            if line.strip() == SrsNoteBlock.END_LINE:
                break
            if line.strip() == SrsNoteBlock.CARD_SEPARATOR_LINE:
                is_front = False
                continue
            if is_front:
                front_lines.append(line)
            else:
                back_lines.append(line)

        return front_lines, back_lines, note_number


def srs_side_as_lines(renderer, token: SrsNoteBlock, is_front: bool) -> list[str]:
    if is_front:
        children: list[Token] = token.children_front
    else:
        children = token.children_back

    # The underlying renderer returns lines with new lines at the end, but then
    # joins them with a new line, duplicating new lines.
    # Strip these, and account for us sometimes returning None.
    lines_raw = list(map(renderer.render, children))
    lines_stripped = [line.removesuffix("\n") for line in lines_raw]
    return lines_stripped


class AnkiRenderer(HtmlRenderer):
    def __init__(self):
        super().__init__(ClozeSpan, SrsNoteBlock, EmptyNewLine)
        self.render_map["BlankLine"] = self.render_blank_line

    def render_empty_new_line(self, token) -> str:
        return ""

    def render_blank_line(self, token, max_line_length=None) -> str:
        return "<br>"

    def render_srs_side(self, token: SrsNoteBlock, is_front: bool) -> str:
        return '\n'.join(srs_side_as_lines(renderer=self, token=token, is_front=is_front))

    def render_srs_note_block(self, token: SrsNoteBlock, max_line_length: int|None = None) -> str:
        del max_line_length  # Duplicate state.
        blocks: list[str] = [
            "<anki-note>",
        ]
        if token.note_number:
            blocks.append(f"<!--note::{token.note_number}-->")
        blocks.append(self.render_srs_side(token, is_front=True))
        if token.children_back:
            blocks.append("<anki-separator />")
            blocks.append(self.render_srs_side(token, is_front=False))
        blocks.append("</anki-note>")
        return "\n".join(blocks)

    def render_cloze_span(self, token: ClozeSpan) -> str:
        # TODO: Debug why there is an added space.
        cloze_text = self.render_inner(token).rstrip()
        content = "{{c" + token.cloze_number + "::" + cloze_text
        if token.cloze_hint:
            content += "::" + token.cloze_hint
        content += "}}"
        return content

class MarkdownRendererWithSrsUpdates(markdown_renderer.MarkdownRenderer):
    def __init__(self):
        super().__init__(ClozeSpan, SrsNoteBlock, EmptyNewLine)

    def render_empty_new_line(self, token, max_line_length: int):
        return [""]

    def render_srs_note_block(self, token: SrsNoteBlock, max_line_length: int) -> markdown_renderer.Iterable[str]:
        del max_line_length  # Duplicate state.
        blocks: list[markdown_renderer.Iterable[str]] = [
            itertools.chain([SrsNoteBlock.START_LINE]),
        ]
        if token.note_number:
            blocks.append([f"<!--note::{token.note_number}-->"])
        blocks.append(itertools.chain(srs_side_as_lines(self, token, is_front=True)))
        if token.children_back:
            blocks.append(itertools.chain([SrsNoteBlock.CARD_SEPARATOR_LINE]))
            blocks.append(itertools.chain(srs_side_as_lines(self, token, is_front=False)))
        blocks.append(itertools.chain([SrsNoteBlock.END_LINE]))
        return itertools.chain.from_iterable(blocks)

    def render_cloze_span(self, token: ClozeSpan) -> markdown_renderer.Iterable[markdown_renderer.Fragment]:
        # TODO: Debug why there is an added space.
        cloze_text = self.render_inner(token).rstrip()
        content = "***<!--c" + token.cloze_number + "::"
        if token.cloze_hint:
            content += token.cloze_hint
        content += "-->" + cloze_text + "***"
        yield markdown_renderer.Fragment(content, wordwrap=True)


class SrsNoteType(Enum):
    FRONT_BACK = 1
    FRONT_BACK_REVERSE = 2
    CLOZE = 3

@dataclass
class SrsNote:
    front: str
    back: str
    type: SrsNoteType
    tag: str
    note_number: int

def is_cloze(note_token: SrsNoteBlock) -> bool:
    tokens: list[Token] = list(note_token.children_front)
    while len(tokens) > 0:
        token = tokens.pop(0)
        if isinstance(token, ClozeSpan):
            return True
        if token.children:
            tokens.extend(list(token.children))
    return False


def compile_notes(
    root_token: block_token.BlockToken,
    renderer: AnkiRenderer,
    print_walk = False,
) -> list[SrsNote]:
    """Does a depth first search of the AST tracking the parent headings and notes."""

    tokens: list[Token] = [root_token]
    notes: list[SrsNote] = []
    headings: list[str] = []
    note_numbers: set[int] = set()

    while len(tokens) > 0:
        token = tokens.pop(0)

        if isinstance(token, (SrsNoteBlock)):
            if is_cloze(token):
                note_type = SrsNoteType.CLOZE
            else:
                note_type = SrsNoteType.FRONT_BACK

            if token.note_number:
                note_number = token.note_number
            else:
                note_number = max(note_numbers) + 1
                token.note_number = note_number
            note_numbers.add(note_number)

            note = SrsNote(
                front=renderer.render_srs_side(token, is_front=True),
                back=renderer.render_srs_side(token, is_front=False),
                type=note_type,
                tag="::".join(headings),
                note_number=note_number,
            )
            notes.append(note)

        if isinstance(token, (block_token.Heading)):
            children: list[Token] = token.children # pyright: ignore[reportAssignmentType]
            if not token.children or not len(children) == 1:
                raise ValueError("Heading must have one child!")
            first_child: span_token.RawText = children[0] # pyright: ignore[reportAssignmentType]
            prefix, _, _ = first_child.content.partition(":")
            prefix = prefix.lower().replace(" ", "_")
            if len(headings) >= token.level:
                headings.pop()  # Previous least important heading
            headings.append(prefix)

        if print_walk:
            print('\nFinished walking token:', type(token), token, token.children, sep="\n")

        # Add children ensuring the walk is in order, and depth first.
        if token.children:
            tokens[0:0] = token.children

    return notes


def create_deck(notes: list[SrsNote], filename_md_apkg: str, dry_run=False):

    cwd = os.getcwd()
    # Get the path to this script, it's parent directory (bin) and it's parent directory.
    repository_location = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

    if cwd != repository_location:
        raise NotImplementedError('For now, this must be run in the repository root!')

    deck_name = "knowledge::" + "::".join(Path(filename_md_apkg.removesuffix(".md.apkg")).parts)
    deck_number: int = (
        int.from_bytes(guid_for("porcoesphino_notes", filename_md_apkg).encode("ascii"))
        % SQL_LITE_MAX_VALUE
    )

    # Documentation:
    # https://github.com/kerrickstaley/genanki
    deck = genanki.Deck(deck_number, deck_name)

    note_number = 0
    for note in notes:
        match note.type:
            case SrsNoteType.CLOZE:
                model = genanki.CLOZE_MODEL
            case SrsNoteType.FRONT_BACK:
                model = genanki.BASIC_MODEL
            case SrsNoteType.FRONT_BACK_REVERSE:
                model = genanki.BASIC_AND_REVERSED_CARD_MODEL
        note = genanki.Note(
            model=model,
            fields=[note.front, note.back],
            tags=[note.tag],
            guid=guid_for("porcoesphino_notes", note.tag, note_number),
        )
        deck.add_note(note)
        note_number += 1

    if not dry_run:
        print(f"Building deck {filename_md_apkg}")
        genanki.Package(deck).write_to_file(filename_md_apkg)
        print("Deck built!")


def parse_markdown_get_notes(filename, print_anki=False, print_walk=False, dry_run=False) -> list[SrsNote]:

    backup_filename = f"{filename}.bak"

    if os.path.isfile(backup_filename):
        raise UserWarning("Backup file already exists")

    if not dry_run:
        print(f"Creating backup at {backup_filename}")
        shutil.copyfile(filename, backup_filename)
        print("File backed up!")

    with open(filename, "r", encoding="utf8") as md_file:
        with AnkiRenderer() as anki_renderer:
            document = mistletoe.Document(md_file)
            notes = compile_notes(
                root_token=document, renderer=anki_renderer, print_walk=print_walk
            )
            if dry_run:
                print("\nParsed notes:\n")
                for note in notes:
                    print(note, "\n")
            if print_anki:
                print(anki_renderer.render(document))
            create_deck(notes, filename_md_apkg=f"{filename}.apkg", dry_run=dry_run)
            with MarkdownRendererWithSrsUpdates() as update_renderer:
                updated_markdown = update_renderer.render(document)


    if not dry_run:
        with open(filename, "w", encoding="utf8") as md_file:
            print(f"Saving metadata to file {filename}")
            md_file.write(updated_markdown)
            print("Metadata saved!")
    else:
        print("\n Updated markdown:\n")
        print(updated_markdown)

    return notes


if __name__ == "__main__":
    parsed_args = validate_and_return_args()
    parsed_notes = parse_markdown_get_notes(
        parsed_args.markdown_filename,
        print_anki=parsed_args.print_anki,
        print_walk=parsed_args.print_walk,
        dry_run=parsed_args.dry_run,
    )
