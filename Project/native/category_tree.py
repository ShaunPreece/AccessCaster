"""The Categories tree: a branch per section, and under it the section's pages when it has more
than one. Every item carries a `pages.Page`; a branch carries its section's first page, so
selecting a section shows that page.

The control is the system's own tree on each platform, because that is the one the screen reader
can use. On Windows that is `wx.TreeCtrl`, the Win32 tree view, where `wx.dataview` draws its own.
On macOS the roles reverse: wx draws `wx.TreeCtrl`, which VoiceOver cannot enter, and
`wx.dataview.DataViewCtrl` is an NSOutlineView. `CategoryTree` is the one this platform uses.
Sections are addressed by their index, in the order they were added. Both offer:

    add_branch(title, pages)    add a section
    set_pages(branch, pages)    replace a section's pages, keeping the selection on the page with
                                the same key where it survives
    select(branch, key=None)    select a section, or its page `key`
    selected()                  (branch, page) of the selection, or (None, None)
    pages(branch)               the pages listed under a section, empty when it has only one
    has_focus()

`on_select(page)` is called when the selection moves, by the user or by `select`, and once after
`set_pages` with whatever is selected then. Never while sections are being added.
"""
import sys

import wx
import wx.dataview as dv


class Win32Tree(wx.TreeCtrl):
    def __init__(self, parent, on_select):
        super().__init__(parent, style=wx.TR_HIDE_ROOT | wx.TR_HAS_BUTTONS
                         | wx.TR_LINES_AT_ROOT | wx.TR_SINGLE)
        self._on_select = on_select
        # Deleting a selected item moves the selection, so a rebuild changes it several times.
        self._quiet = False
        self._root = self.AddRoot("")
        self._branches = []
        self.Bind(wx.EVT_TREE_SEL_CHANGED, self._changed)

    def add_branch(self, title, pages):
        self._quiet = True
        try:
            self._branches.append(self.AppendItem(self._root, title))
            self._fill(self._branches[-1], pages)
        finally:
            self._quiet = False

    def set_pages(self, index, pages):
        branch = self._branches[index]
        selected = self.GetSelection()
        inside = selected.IsOk() and self.GetItemParent(selected) == branch
        key = self.GetItemData(selected).key if inside else None
        self._quiet = True
        try:
            self.DeleteChildren(branch)
            self._fill(branch, pages)
            if inside:
                self.SelectItem(self._find(index, key))
        finally:
            self._quiet = False
        current = self.GetSelection()
        if current.IsOk():
            self._on_select(self.GetItemData(current))

    def select(self, index, key=None):
        self.SelectItem(self._find(index, key))

    def selected(self):
        item = self.GetSelection()
        if not item.IsOk():
            return None, None
        parent = self.GetItemParent(item)
        branch = item if parent == self._root else parent
        return self._branches.index(branch), self.GetItemData(item)

    def pages(self, index):
        return [self.GetItemData(child) for child in self._children(self._branches[index])]

    def has_focus(self):
        return wx.Window.FindFocus() is self

    def _fill(self, branch, pages):
        self.SetItemData(branch, pages[0])
        if len(pages) > 1:
            for page in pages:
                self.AppendItem(branch, page.title, data=page)

    def _find(self, index, key):
        branch = self._branches[index]
        if key is None:
            return branch
        return next((child for child in self._children(branch)
                     if self.GetItemData(child).key == key), branch)

    def _children(self, item):
        children = []
        child, cookie = self.GetFirstChild(item)
        while child.IsOk():
            children.append(child)
            child, cookie = self.GetNextChild(item, cookie)
        return children

    def _changed(self, event):
        # The selection also moves as the items are deleted with the window.
        if self._quiet or not self or self.IsBeingDeleted():
            return
        item = event.GetItem()
        if item.IsOk():
            self._on_select(self.GetItemData(item))


class _Node:
    def __init__(self, title, page, parent=None):
        self.title = title
        self.page = page
        self.parent = parent
        self.children = []


class _Model(dv.PyDataViewModel):
    """The tree's content: one column, the title. An item is a container only while it has
    children, so a section with a single page has no expand arrow."""

    def __init__(self):
        super().__init__()
        self.UseWeakRefs(True)
        self.branches = []

    def GetColumnCount(self):
        return 1

    def GetColumnType(self, _column):
        return "string"

    def GetChildren(self, parent, children):
        nodes = self.ItemToObject(parent).children if parent.IsOk() else self.branches
        for node in nodes:
            children.append(self.ObjectToItem(node))
        return len(nodes)

    def IsContainer(self, item):
        return not item.IsOk() or bool(self.ItemToObject(item).children)

    def GetParent(self, item):
        parent = self.ItemToObject(item).parent if item.IsOk() else None
        return self.ObjectToItem(parent) if parent else dv.NullDataViewItem

    def GetValue(self, item, _column):
        return self.ItemToObject(item).title

    def SetValue(self, _value, _item, _column):
        return False


class OutlineTree(dv.DataViewCtrl):
    def __init__(self, parent, on_select):
        super().__init__(parent, style=dv.DV_NO_HEADER | dv.DV_SINGLE)
        self._model = _Model()
        self.AssociateModel(self._model)
        self.AppendTextColumn("Categories", 0)
        self._on_select = on_select
        self._quiet = False
        # wx may or may not report a selection made by code, so `select` reports it and this
        # stops it being reported twice.
        self._reported = None
        self.Bind(dv.EVT_DATAVIEW_SELECTION_CHANGED, self._changed)

    def add_branch(self, title, pages):
        branch = _Node(title, pages[0])
        self._fill(branch, pages)
        self._model.branches.append(branch)
        self._quiet = True
        try:
            self._model.ItemAdded(dv.NullDataViewItem, self._item(branch))
        finally:
            self._quiet = False

    def set_pages(self, index, pages):
        branch = self._model.branches[index]
        current = self._selected_node()
        key = current.page.key if current is not None and current.parent is branch else None
        expanded = [b for b in self._model.branches if self.IsExpanded(self._item(b))]
        self._quiet = True
        try:
            self._fill(branch, pages)
            # A whole reload, because a branch can change between leaf and container here, and
            # NSOutlineView only asks again whether an item can expand when its parent reloads.
            self._model.Cleared()
            for b in expanded:
                if b.children:
                    self.Expand(self._item(b))
            if current is not None:
                self._select_node(self._find(index, key) if current.parent is branch
                                  else current)
        finally:
            self._quiet = False
        node = self._selected_node()
        if node is not None:
            self._reported = node
            self._on_select(node.page)

    def select(self, index, key=None):
        node = self._find(index, key)
        self._select_node(node)
        self._report(node)

    def selected(self):
        node = self._selected_node()
        if node is None:
            return None, None
        return self._model.branches.index(node.parent or node), node.page

    def pages(self, index):
        return [child.page for child in self._model.branches[index].children]

    def has_focus(self):
        focus = wx.Window.FindFocus()
        return focus is not None and (focus is self or self.IsDescendant(focus))

    def _fill(self, branch, pages):
        branch.page = pages[0]
        branch.children = ([_Node(page.title, page, branch) for page in pages]
                           if len(pages) > 1 else [])

    def _find(self, index, key):
        branch = self._model.branches[index]
        if key is None:
            return branch
        return next((child for child in branch.children if child.page.key == key), branch)

    def _item(self, node):
        return self._model.ObjectToItem(node)

    def _selected_node(self):
        item = self.GetSelection()
        return self._model.ItemToObject(item) if item.IsOk() else None

    def _select_node(self, node):
        if node.parent is not None:
            self.Expand(self._item(node.parent))
        item = self._item(node)
        self.Select(item)
        self.SetCurrentItem(item)
        self.EnsureVisible(item)

    def _report(self, node):
        if node is not self._reported:
            self._reported = node
            self._on_select(node.page)

    def _changed(self, _event):
        node = self._selected_node()
        if not self._quiet and node is not None:
            self._report(node)


CategoryTree = OutlineTree if sys.platform == "darwin" else Win32Tree
