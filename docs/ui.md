# UI API Reference

This document is automatically generated. Do not edit manually.

## Component Architecture & Catalog Diagrams

### Catalog Interactive Workbench Workflow

```mermaid
graph TD
    workbench("Catalog Workbench")
    selector["Component Selector"]
    viewport["Viewport Controller"]
    state_var["State Variant Switcher"]
    renderer[["Component Renderer"]]
    a11y_scan{Accessibility Auditor}
    preview("Interactive Viewport Frame")
    workbench --> selector
    workbench --> viewport
    workbench --> state_var
    selector --> renderer
    viewport --> preview
    state_var --> renderer
    renderer --> preview
    renderer --> a11y_scan
    click workbench "docs/ui.md" "Catalog Workbench"
    click selector "docs/ui.md" "Component Selector"
    click viewport "docs/ui.md" "Viewport Controller"
    click state_var "docs/ui.md" "State Variant Switcher"
    click renderer "docs/ui.md" "Component Renderer"
    click a11y_scan "docs/admin_guide.md" "Accessibility Auditor"
    click preview "docs/ui.md" "Interactive Viewport Frame"
```

### UI Component Hierarchy

```mermaid
graph LR
    subgraph nav ["Navigation & Layout"]
        tui_app["Textual Application Shell"]
        heading["Heading Component"]
    end
    subgraph inputs ["Selection & Controls"]
        vim_tree["VimTree Plan View"]
        density_slider["Density Slider Widget"]
        path_diff["Path Diff Comparison"]
    end
    subgraph views ["Display & Modals"]
        edit_modal["Edit & Reassign Modal"]
        settings_modal["Settings Modal"]
        setup_wizard["Setup Wizard Modal"]
    end
    tui_app --> heading
    tui_app --> vim_tree
    vim_tree --> edit_modal
    vim_tree --> path_diff
    tui_app --> settings_modal
    settings_modal --> setup_wizard
    click tui_app "docs/ui.md#appuitui" "Main Terminal UI Container"
    click heading "docs/ui.md#appuiheading" "Header & Title Bar"
    click vim_tree "docs/ui.md#appuiwidgetsvim_tree" "Interactive Directory Plan Tree"
    click density_slider "docs/ui.md#appuiwidgetsdensity_slider" "Layout Density Control"
    click path_diff "docs/ui.md#appuiwidgetspath_diff" "Path Diff Preview Component"
    click edit_modal "docs/ui.md#appuimodalsedit" "File Reassignment and Node Edit Dialog"
    click settings_modal "docs/ui.md#appuimodalssettings" "Application Settings Dialog"
    click setup_wizard "docs/ui.md#appuimodalswizard" "First-Run Configuration Wizard"
```

## `app.ui.a11y`

::: app.ui.a11y

## `app.ui.ai_runtime`

::: app.ui.ai_runtime

## `app.ui.catalog`

::: app.ui.catalog

## `app.ui.heading`

::: app.ui.heading

## `app.ui.layout`

::: app.ui.layout

## `app.ui.markup_utils`

::: app.ui.markup_utils

## `app.ui.mixins.pipeline`

::: app.ui.mixins.pipeline

## `app.ui.mixins.selection`

::: app.ui.mixins.selection

## `app.ui.mixins.tree_view`

::: app.ui.mixins.tree_view

## `app.ui.modals.command_palette`

::: app.ui.modals.command_palette

## `app.ui.modals.drop_zone`

::: app.ui.modals.drop_zone

## `app.ui.modals.edit`

::: app.ui.modals.edit

## `app.ui.modals.help`

::: app.ui.modals.help

## `app.ui.modals.models`

::: app.ui.modals.models

## `app.ui.modals.quarantine`

::: app.ui.modals.quarantine

## `app.ui.modals.reports`

::: app.ui.modals.reports

## `app.ui.modals.rules`

::: app.ui.modals.rules

## `app.ui.modals.sample_playground`

::: app.ui.modals.sample_playground

## `app.ui.modals.session`

::: app.ui.modals.session

## `app.ui.modals.settings`

::: app.ui.modals.settings

## `app.ui.modals.wizard`

::: app.ui.modals.wizard

## `app.ui.notifications`

::: app.ui.notifications

## `app.ui.protocols`

::: app.ui.protocols

## `app.ui.tree_search`

::: app.ui.tree_search

## `app.ui.tui`

::: app.ui.tui

## `app.ui.widgets.density_slider`

::: app.ui.widgets.density_slider

## `app.ui.widgets.labeled_switch`

::: app.ui.widgets.labeled_switch

## `app.ui.widgets.path_diff`

::: app.ui.widgets.path_diff

## `app.ui.widgets.vim_tree`

::: app.ui.widgets.vim_tree

