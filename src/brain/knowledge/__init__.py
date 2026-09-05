"""Knowledge: documents, their chunks, and what a person may be told from them.

A chunk carries the permissions of the document it came from. Retrieval that forgot
that would answer from a paragraph nobody was allowed to read, and the answer would
look exactly like a correct one.

**What does not belong here.** Business records. A record is a row with an owner and
a scope; a document is a file with a permission set, and conflating them means one
of the two mechanisms stops being applied.
"""
