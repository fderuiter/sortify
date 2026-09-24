# UI API Reference

This document is automatically generated. Do not edit manually.

## Component Architecture & Catalog Diagrams

### Catalog Interactive Workbench Workflow

![Component Catalog Workflow](assets/diagrams/catalog_workflow.svg)

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
```

### UI Component Hierarchy

![UI Component Hierarchy](assets/diagrams/ui_component_hierarchy.svg)

```mermaid
graph LR
    subgraph nav ["Navigation Components"]
        header_bar["Application Header Bar"]
        toolbar["Top Action Toolbar"]
    end
    subgraph inputs ["Selection & Controls"]
        directory_selection["Directory Selection Card"]
        settings_modal["Settings Dialog View"]
        setup_wizard["AI Model Setup Wizard"]
    end
    subgraph views ["Display & Audits"]
        plan_treeview["Proposed Reorganization Plan"]
        cro_forensic_dialog["CRO Forensic View"]
        status_progress_panel["Status & Progress Panel"]
    end
    header_bar --> directory_selection
    directory_selection --> plan_treeview
    plan_treeview --> status_progress_panel
    settings_modal --> setup_wizard
    cro_forensic_dialog --> status_progress_panel
```

## `app.ui.a11y_runner`

::: app.ui.a11y_runner

## `app.ui.app`

::: app.ui.app

## `app.ui.catalog`

::: app.ui.catalog

## `app.ui.cro_forensic_view`

::: app.ui.cro_forensic_view

## `app.ui.diagram_schema`

::: app.ui.diagram_schema

## `app.ui.dialog_helper`

::: app.ui.dialog_helper

## `app.ui.help_modal`

::: app.ui.help_modal

## `app.ui.settings`

::: app.ui.settings

## `app.ui.tokens`

::: app.ui.tokens

## `app.ui.toolbar`

::: app.ui.toolbar

## `app.ui.tui`

::: app.ui.tui

## `app.ui.wizard`

::: app.ui.wizard

