import json

class HeadlessTreeview:
    def __init__(self, *args, **kwargs):
        self._nodes = {}  # iid -> { "text": ..., "parent": ..., "children": [], "open": ..., ... }
        self._nodes[""] = {"text": "root", "parent": None, "children": [], "open": True}
        self.pack = lambda *a, **k: None
        self.configure = lambda *a, **k: None
        self.bind = lambda *a, **k: None
        self.yview = lambda *a, **k: None

    def insert(self, parent, index, iid=None, text="", **kwargs):
        if iid is None:
            iid = f"item_{len(self._nodes)}"
        if iid in self._nodes:
            raise ValueError(f"Item {iid} already exists")
        
        self._nodes[iid] = {
            "text": text,
            "parent": parent,
            "children": [],
            "open": kwargs.get("open", False),
            **kwargs
        }
        
        if parent not in self._nodes:
            parent = ""
            self._nodes[iid]["parent"] = ""

        if index == "end":
            self._nodes[parent]["children"].append(iid)
        else:
            self._nodes[parent]["children"].insert(0, iid)
        
        return iid

    def delete(self, *items):
        for item in items:
            self._delete_recursive(item)

    def _delete_recursive(self, item):
        if item not in self._nodes:
            return
        
        for child in list(self._nodes[item]["children"]):
            self._delete_recursive(child)
            
        parent = self._nodes[item]["parent"]
        if parent is not None and item in self._nodes[parent]["children"]:
            self._nodes[parent]["children"].remove(item)
            
        del self._nodes[item]

    def get_children(self, item=""):
        if item not in self._nodes:
            return ()
        return tuple(self._nodes[item]["children"])

    def item(self, item, option=None, **kwargs):
        if item not in self._nodes:
            return {}
        
        if kwargs:
            for k, v in kwargs.items():
                self._nodes[item][k] = v
                
        if option is not None:
            return self._nodes[item].get(option)
            
        return self._nodes[item]

    def parent(self, item):
        if item not in self._nodes:
            return ""
        return self._nodes[item]["parent"]

    def move(self, item, parent, index):
        if item not in self._nodes:
            return
            
        old_parent = self._nodes[item]["parent"]
        if item in self._nodes[old_parent]["children"]:
            self._nodes[old_parent]["children"].remove(item)
            
        self._nodes[item]["parent"] = parent
        if parent not in self._nodes:
            parent = ""
            
        if index == "end":
            self._nodes[parent]["children"].append(item)
        else:
            self._nodes[parent]["children"].insert(0, item)

    def identify_row(self, y):
        return None

    def dump_state(self, root=""):
        """Generate a JSON-serializable dictionary representation of the tree hierarchy."""
        def build_tree(node_id):
            node = self._nodes[node_id]
            result = {
                "iid": node_id,
                "text": node["text"],
                "children": []
            }
            if "open" in node and node["open"]:
                result["open"] = node["open"]
                
            for child_id in node["children"]:
                result["children"].append(build_tree(child_id))
            return result
            
        res = build_tree(root)
        return res["children"] if root == "" else res

